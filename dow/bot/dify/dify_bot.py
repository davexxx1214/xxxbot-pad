# encoding:utf-8
import io
import os
import mimetypes
import threading
import json


import requests
from urllib.parse import urlparse, unquote

from bot.bot import Bot
from lib.dify.dify_client import DifyClient, ChatClient
from bot.dify.dify_session import DifySession, DifySessionManager
from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from common.log import logger
from common import const, memory
from common.utils import parse_markdown_text, print_red
from common.tmp_dir import TmpDir
from config import conf
from bot.openai.open_ai_image import OpenAIImage

UNKNOWN_ERROR_MSG = "我暂时遇到了一些问题，请您稍后重试~"

class DifyBot(Bot):
    def __init__(self):
        super().__init__()
        self.sessions = DifySessionManager(DifySession, model=conf().get("model", const.DIFY))
        self.image_creator = OpenAIImage()  # 初始化OpenAIImage
        self.image_create_prefix = conf().get("image_create_prefix", ["画"])  # 从配置读取画图触发词

    def reply(self, query, context: Context=None):
        # acquire reply content
        if context.type == ContextType.TEXT or context.type == ContextType.IMAGE_CREATE:
            if context.type == ContextType.IMAGE_CREATE:
                query = conf().get('image_create_prefix', ['画'])[0] + query
            logger.info("[DIFY] query={}".format(query))
            session_id = context["session_id"]
            # TODO: 适配除微信以外的其他channel
            channel_type = conf().get("channel_type", "wx")
            user = None
            if channel_type in ["wx", "wework", "gewechat", "wx849"]:
                user = context["msg"].other_user_nickname if context.get("msg") else "default"
            elif channel_type in ["wechatcom_app", "wechatmp", "wechatmp_service", "wechatcom_service", "web"]:
                user = context["msg"].other_user_id if context.get("msg") else "default"
            else:
                return Reply(ReplyType.ERROR, f"unsupported channel type: {channel_type}, now dify only support wx, wx849, wechatcom_app, wechatmp, wechatmp_service channel")
            logger.debug(f"[DIFY] dify_user={user}")
            user = user if user else "default" # 防止用户名为None，当被邀请进的群未设置群名称时用户名为None
            session = self.sessions.get_session(session_id, user)
            if context.get("isgroup", False):
                # 群聊：根据是否是共享会话群来决定是否设置用户信息
                if not context.get("is_shared_session_group", False):
                    # 非共享会话群：设置发送者信息
                    session.set_user_info(context["msg"].actual_user_id, context["msg"].actual_user_nickname)
                else:
                    # 共享会话群：不设置用户信息
                    session.set_user_info('', '')
                # 设置群聊信息
                session.set_room_info(context["msg"].other_user_id, context["msg"].other_user_nickname)
            else:
                # 私聊：使用发送者信息作为用户信息，房间信息留空
                session.set_user_info(context["msg"].other_user_id, context["msg"].other_user_nickname)
                session.set_room_info('', '')

            # 打印设置的session信息
            logger.debug(f"[DIFY] Session user and room info - user_id: {session.get_user_id()}, user_name: {session.get_user_name()}, room_id: {session.get_room_id()}, room_name: {session.get_room_name()}")
            logger.debug(f"[DIFY] session={session} query={query}")

            reply, err = self._reply(query, session, context)
            if err != None:
                dify_error_reply = conf().get("dify_error_reply", None)
                error_msg = dify_error_reply if dify_error_reply else err
                reply = Reply(ReplyType.TEXT, error_msg)
            return reply
        else:
            reply = Reply(ReplyType.ERROR, "Bot不支持处理{}类型的消息".format(context.type))
            return reply

    # TODO: delete this function
    def _get_payload(self, query, session: DifySession, response_mode):
        # 输入的变量参考 wechat-assistant-pro：https://github.com/leochen-g/wechat-assistant-pro/issues/76
        return {
            'inputs': {
                'user_id': session.get_user_id(),
                'user_name': session.get_user_name(),
                'room_id': session.get_room_id(),
                'room_name': session.get_room_name()
            },
            "query": query,
            "response_mode": response_mode,
            "conversation_id": session.get_conversation_id(),
            "user": session.get_user()
        }

    def _get_dify_conf(self, context: Context, key, default=None):
        return context.get(key, conf().get(key, default))

    def _reply(self, query: str, session: DifySession, context: Context):

        query = query.strip()
        for prefix in self.image_create_prefix:
            if query.startswith(prefix):
                # 提取画图提示词
                prompt = query[len(prefix):].strip()
                logger.info(f"[DIFY] 检测到画图请求，触发词={prefix}，提示词={prompt}")
                # 调用OpenAIImage创建图片
                success, result = self.image_creator.create_img(prompt, context=context)
                if success:
                    try:
                        # 使用 BytesIO 读取图片文件
                        from io import BytesIO
                        from PIL import Image
                        
                        # 打开图片文件
                        img = Image.open(result)
                        img_io = BytesIO()
                        
                        # 根据图片格式保存
                        if img.format == 'PNG':
                            img.save(img_io, format='PNG')
                        else:
                            img = img.convert('RGB')
                            img.save(img_io, format='JPEG')
                        
                        img_io.seek(0)
                        return Reply(ReplyType.IMAGE, img_io), None
                    except Exception as e:
                        logger.error(f"[DIFY] 处理图片文件失败: {e}")
                        # 如果处理失败，尝试直接返回文件路径
                        return Reply(ReplyType.IMAGE, result), None
                else:
                    return Reply(ReplyType.TEXT, result), None
                
        try:
            # 检查是否包含深度搜索关键词
            deepsearch_keywords = ["深度搜索", "深度研究","深入研究","深度推理"]
            for keyword in deepsearch_keywords:
                if query.startswith(keyword):
                    # 截掉关键词，获取实际查询内容
                    actual_query = query[len(keyword):].strip()
                    logger.info(f"[DIFY] 检测到深度搜索请求: 关键词={keyword}, 实际查询={actual_query}")
                    deepsearch_model = conf().get("deepsearch_model", "sonar-reasoning-pro")
                    return self._use_specific_model(actual_query, context, deepsearch_model)            

            session.count_user_message() # 限制一个conversation中消息数，防止conversation过长
            dify_app_type = self._get_dify_conf(context, "dify_app_type", 'chatbot')
            if dify_app_type == 'chatbot' or dify_app_type == 'chatflow':
                return self._handle_chatbot(query, session, context)
            elif dify_app_type == 'agent':
                return self._handle_agent(query, session, context)
            elif dify_app_type == 'workflow':
                return self._handle_workflow(query, session, context)
            else:
                friendly_error_msg = "[DIFY] 请检查 config.json 中的 dify_app_type 设置，目前仅支持 agent, chatbot, chatflow, workflow"
                return None, friendly_error_msg

        except Exception as e:
            error_info = f"[DIFY] Exception: {e}"
            logger.exception(error_info)
            return None, UNKNOWN_ERROR_MSG

    def _use_specific_model(self, query, context, model_name):
        """使用指定的模型处理请求"""
        try:
            logger.info(f"[DIFY] 使用深度搜索模型处理请求: {model_name}")
            import openai
            from bot.chatgpt.chat_gpt_bot import ChatGPTBot

            # 先创建实例
            specific_bot = ChatGPTBot()

            # 保存原始的API配置
            original_api_key = openai.api_key
            original_api_base = openai.api_base
            original_proxy = openai.proxy # 保存原始代理设置

            try:
                # ... (设置 deepsearch API 配置的代码保持不变) ...
                # 获取深度搜索的特定API配置，如果未配置则使用默认OpenAI配置
                deepsearch_api_key = conf().get("deepsearch_api_key", conf().get("open_ai_api_key"))
                deepsearch_api_base = conf().get("deepsearch_api_base", conf().get("open_ai_api_base"))

                # 设置OpenAI配置 - 这会影响全局配置
                openai.api_key = deepsearch_api_key
                if deepsearch_api_base:
                    openai.api_base = deepsearch_api_base
                else:
                    # 如果深度搜索没有指定base，确保使用原始base或默认base
                    openai.api_base = original_api_base if original_api_base else conf().get("open_ai_api_base")

                logger.info(f"[DIFY] DeepSearch 使用API Base: {openai.api_base}，Key: {openai.api_key[:3]}...{openai.api_key[-3:]}")
                # 如果有设置代理，也打印出来
                if openai.proxy:
                        logger.info(f"[DIFY] DeepSearch 使用 Proxy: {openai.proxy}")

                # 创建新的上下文
                specific_context = Context(type=ContextType.TEXT, content=query) # 直接设置 content

                # 显式复制必要的属性，尤其是 session_id
                if context:
                    # 确保 session_id 被复制
                    if "session_id" in context:
                        specific_context["session_id"] = context["session_id"]
                        logger.debug(f"[DIFY] Copied session_id to specific_context: {context['session_id']}") # 增加日志确认复制
                    else:
                         # 如果原始 context 确实没有 session_id，这是一个严重问题
                         logger.error("[DIFY] Original context is missing 'session_id' in _use_specific_model!")
                         return None, "内部错误：缺少会话ID"

                    # 复制其他可能被 chatgpt_bot.reply 使用的属性 (可选)
                    for key in ["msg", "isgroup", "receiver"]: # 按需添加
                        if key in context:
                            specific_context[key] = context[key]
                else:
                     logger.error("[DIFY] Original context is None in _use_specific_model!")
                     return None, "内部错误：缺少上下文信息"

                # 设置gpt_model
                specific_context["gpt_model"] = model_name

                # 使用ChatGPTBot处理请求 (现在 specific_context 应该有 session_id)
                reply = specific_bot.reply(query, specific_context)

                logger.info(f"[DIFY] 使用模型 {model_name} 处理成功")
                return reply, None
            finally:
                # ... (恢复原始 API 配置的代码保持不变) ...
                # 恢复原始的API配置
                openai.api_key = original_api_key
                openai.api_base = original_api_base
                openai.proxy = original_proxy # 恢复原始代理设置
                logger.debug("[DIFY] 恢复原始OpenAI API及代理配置")

        except Exception as e:
            # ... (异常处理和 failover 代码保持不变) ...
            # 如果特定模型失败，尝试使用故障转移模型
            logger.exception(f"[DIFY] 特定模型处理失败: {e}，尝试使用故障转移模型")
            # 在调用故障转移前，确保OpenAI配置已恢复或设置为故障转移所需状态
            # (上面的 finally 块已经做了恢复，所以这里应该是安全的)
            return self._use_failover_bot(query, context) 


    def _handle_chatbot(self, query: str, session: DifySession, context: Context):
        try:
            api_key = self._get_dify_conf(context, "dify_api_key", '')
            api_base = self._get_dify_conf(context, "dify_api_base", "https://api.dify.ai/v1")
            chat_client = ChatClient(api_key, api_base)
            response_mode = 'blocking'
            payload = self._get_payload(query, session, response_mode)
            files = self._get_upload_files(session, context)
            response = chat_client.create_chat_message(
                inputs=payload['inputs'],
                query=payload['query'],
                user=payload['user'],
                response_mode=payload['response_mode'],
                conversation_id=payload['conversation_id'],
                files=files
            )

            if response.status_code != 200:
                error_info = f"[DIFY] payload={payload} response text={response.text} status_code={response.status_code}"
                logger.warning(error_info)
                
                # 使用ChatGPTBot作为故障转移
                logger.info("[DIFY] API返回非200状态码，启动故障转移到ChatGPTBot")
                return self._use_failover_bot(query, context)

            rsp_data = response.json()
            logger.debug("[DIFY] usage {}".format(rsp_data.get('metadata', {}).get('usage', 0)))

            answer = rsp_data['answer']
            
            # 直接使用answer作为回复内容，不进行markdown解析
            # 不在这里添加@前缀，让gewechat_channel处理
            reply = Reply(ReplyType.TEXT, answer)
            
            # 设置dify conversation_id, 依靠dify管理上下文
            if session.get_conversation_id() == '':
                session.set_conversation_id(rsp_data['conversation_id'])

            return reply, None
        except Exception as e:
            # 记录错误信息
            error_info = f"[DIFY] Exception in _handle_chatbot: {e}"
            logger.exception(error_info)
            
            # 使用ChatGPTBot作为故障转移
            logger.info("[DIFY] 发生异常，启动故障转移到ChatGPTBot")
            return self._use_failover_bot(query, context)
            
    def _use_failover_bot(self, query, context):
        """使用ChatGPTBot作为故障转移处理请求"""
        try:
            logger.info("[DIFY] Failover to ChatGPTBot")
            import openai
            from bot.chatgpt.chat_gpt_bot import ChatGPTBot

            # 先创建实例 (它会读取一次默认配置，没关系)
            failover_bot = ChatGPTBot()

            # 使用专门的故障转移API配置
            failover_api_key = conf().get("failover_api_key", conf().get("open_ai_api_key"))
            failover_api_base = conf().get("failover_api_base", conf().get("open_ai_api_base"))

            # 保存原始的API配置
            original_api_key = openai.api_key
            original_api_base = openai.api_base
            original_proxy = openai.proxy # 保存原始代理

            try:
                # 设置Failover的OpenAI配置 - 这会影响全局配置
                openai.api_key = failover_api_key
                if failover_api_base:
                    openai.api_base = failover_api_base
                else:
                     # 如果没有指定failover base，确保使用原始base或默认base
                     openai.api_base = original_api_base if original_api_base else conf().get("open_ai_api_base")

                # 对于 failover，通常应该使用全局代理设置
                proxy = conf().get("proxy")
                if proxy:
                    openai.proxy = proxy
                else:
                    openai.proxy = None # 确保如果没有配置全局代理，则清除代理设置

                logger.info(f"[DIFY] Failover using API base: {openai.api_base} with key: {failover_api_key[:3]}...{failover_api_key[-3:]}")
                if openai.proxy:
                     logger.info(f"[DIFY] Failover using Proxy: {openai.proxy}")

                # 使用配置中的failover_model
                failover_model = conf().get("failover_model", "gpt-3.5-turbo")

                # 创建新的上下文，正确复制原始context的所有属性
                failover_context = Context(type=ContextType.TEXT, content=query) # 直接设置 content

                # 显式复制必要的属性，尤其是 session_id
                if context:
                    # 确保 session_id 被复制
                    if "session_id" in context:
                        failover_context["session_id"] = context["session_id"]
                        logger.debug(f"[DIFY] Copied session_id to failover_context: {context['session_id']}") # 增加日志确认复制
                    else:
                        # 如果原始 context 确实没有 session_id，这是一个严重问题
                        logger.error("[DIFY] Original context is missing 'session_id' in _use_failover_bot!")
                        return None, "内部错误：缺少会话ID"

                    # 复制其他可能被 chatgpt_bot.reply 使用的属性 (可选)
                    for key in ["msg", "isgroup", "receiver"]: # 按需添加
                        if key in context:
                            failover_context[key] = context[key]
                else:
                    logger.error("[DIFY] Original context is None in _use_failover_bot!")
                    return None, "内部错误：缺少上下文信息"

                # 设置gpt_model
                failover_context["gpt_model"] = failover_model

                # 使用ChatGPTBot处理请求 (此时openai配置是failover的)
                reply = failover_bot.reply(query, failover_context)

                logger.info(f"[DIFY] Failover successful using model: {failover_model}")
                return reply, None
            finally:
                # 恢复原始的API配置
                openai.api_key = original_api_key
                openai.api_base = original_api_base
                openai.proxy = original_proxy # 恢复原始代理设置
                logger.debug("[DIFY] 恢复原始OpenAI API及代理配置")

        except Exception as failover_e:
            # 如果故障转移也失败，记录错误并返回默认错误消息
            logger.exception(f"[DIFY] Failover failed: {failover_e}")
            return None, UNKNOWN_ERROR_MSG


    def _download_file(self, url):
        try:
            response = requests.get(url)
            response.raise_for_status()
            parsed_url = urlparse(url)
            logger.debug(f"Downloading file from {url}")
            url_path = unquote(parsed_url.path)
            # 从路径中提取文件名
            file_name = url_path.split('/')[-1]
            logger.debug(f"Saving file as {file_name}")
            file_path = os.path.join(TmpDir().path(), file_name)
            with open(file_path, 'wb') as file:
                file.write(response.content)
            return file_path
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
        return None

    def _download_image(self, url):
        try:
            pic_res = requests.get(url, stream=True)
            pic_res.raise_for_status()
            image_storage = io.BytesIO()
            size = 0
            for block in pic_res.iter_content(1024):
                size += len(block)
                image_storage.write(block)
            logger.debug(f"[WX] download image success, size={size}, img_url={url}")
            image_storage.seek(0)
            return image_storage
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
        return None

    def _handle_agent(self, query: str, session: DifySession, context: Context):
        try:
            api_key = self._get_dify_conf(context, "dify_api_key", '')
            api_base = self._get_dify_conf(context, "dify_api_base", "https://api.dify.ai/v1")
            chat_client = ChatClient(api_key, api_base)
            response_mode = 'streaming'
            payload = self._get_payload(query, session, response_mode)
            files = self._get_upload_files(session, context)
            response = chat_client.create_chat_message(
                inputs=payload['inputs'],
                query=payload['query'],
                user= payload['user'],
                response_mode=payload['response_mode'],
                conversation_id=payload['conversation_id'],
                files=files
            )

            if response.status_code != 200:
                error_info = f"[DIFY] payload={payload} response text={response.text} status_code={response.status_code}"
                logger.warning(error_info)
                friendly_error_msg = self._handle_error_response(response.text, response.status_code)
                return None, friendly_error_msg
            # response:
            # data: {"event": "agent_thought", "id": "8dcf3648-fbad-407a-85dd-73a6f43aeb9f", "task_id": "9cf1ddd7-f94b-459b-b942-b77b26c59e9b", "message_id": "1fb10045-55fd-4040-99e6-d048d07cbad3", "position": 1, "thought": "", "observation": "", "tool": "", "tool_input": "", "created_at": 1705639511, "message_files": [], "conversation_id": "c216c595-2d89-438c-b33c-aae5ddddd142"}
            # data: {"event": "agent_thought", "id": "8dcf3648-fbad-407a-85dd-73a6f43aeb9f", "task_id": "9cf1ddd7-f94b-459b-b942-b77b26c59e9b", "message_id": "1fb10045-55fd-4040-99e6-d048d07cbad3", "position": 1, "thought": "", "observation": "", "tool": "dalle3", "tool_input": "{\"dalle3\": {\"prompt\": \"cute Japanese anime girl with white hair, blue eyes, bunny girl suit\"}}", "created_at": 1705639511, "message_files": [], "conversation_id": "c216c595-2d89-438c-b33c-aae5ddddd142"}
            # data: {"event": "agent_message", "id": "1fb10045-55fd-4040-99e6-d048d07cbad3", "task_id": "9cf1ddd7-f94b-459b-b942-b77b26c59e9b", "message_id": "1fb10045-55fd-4040-99e6-d048d07cbad3", "answer": "I have created an image of a cute Japanese", "created_at": 1705639511, "conversation_id": "c216c595-2d89-438c-b33c-aae5ddddd142"}
            # data: {"event": "message_end", "task_id": "9cf1ddd7-f94b-459b-b942-b77b26c59e9b", "id": "1fb10045-55fd-4040-99e6-d048d07cbad3", "message_id": "1fb10045-55fd-4040-99e6-d048d07cbad3", "conversation_id": "c216c595-2d89-438c-b33c-aae5ddddd142", "metadata": {"usage": {"prompt_tokens": 305, "prompt_unit_price": "0.001", "prompt_price_unit": "0.001", "prompt_price": "0.0003050", "completion_tokens": 97, "completion_unit_price": "0.002", "completion_price_unit": "0.001", "completion_price": "0.0001940", "total_tokens": 184, "total_price": "0.0002290", "currency": "USD", "latency": 1.771092874929309}}}
            msgs, conversation_id = self._handle_sse_response(response)
            channel = context.get("channel")
            # TODO: 适配除微信以外的其他channel
            is_group = context.get("isgroup", False)
            for msg in msgs[:-1]:
                if msg['type'] == 'agent_message':
                    # 不在这里添加@前缀，让gewechat_channel处理
                    reply = Reply(ReplyType.TEXT, msg['content'])
                    channel.send(reply, context)
                elif msg['type'] == 'message_file':
                    url = self._fill_file_base_url(msg['content']['url'])
                    reply = Reply(ReplyType.IMAGE_URL, url)
                    thread = threading.Thread(target=channel.send, args=(reply, context))
                    thread.start()
            final_msg = msgs[-1]
            reply = None
            if final_msg['type'] == 'agent_message':
                reply = Reply(ReplyType.TEXT, final_msg['content'])
            elif final_msg['type'] == 'message_file':
                url = self._fill_file_base_url(final_msg['content']['url'])
                reply = Reply(ReplyType.IMAGE_URL, url)
            # 设置dify conversation_id, 依靠dify管理上下文
            if session.get_conversation_id() == '':
                session.set_conversation_id(conversation_id)
            return reply, None
        except Exception as e:
            # 记录错误信息
            error_info = f"[DIFY] Exception in _handle_agent: {e}"
            logger.exception(error_info)
            
            # 使用ChatGPTBot作为故障转移
            try:
                logger.info("[DIFY] Failover to ChatGPTBot")
                from bot.chatgpt.chat_gpt_bot import ChatGPTBot
                
                # 创建ChatGPTBot实例
                failover_bot = ChatGPTBot()
                
                # 使用配置中的failover_model
                failover_model = conf().get("failover_model", "gpt-3.5-turbo")
                
                # 创建新的上下文，正确复制原始context的所有属性
                failover_context = Context(type=ContextType.TEXT)
                
                # 如果原始context存在，复制其content和kwargs
                if context:
                    failover_context.content = context.content
                    for key, value in context.kwargs.items():
                        failover_context[key] = value
                    
                # 设置gpt_model
                failover_context["gpt_model"] = failover_model
                
                # 使用ChatGPTBot处理请求
                reply = failover_bot.reply(query, failover_context)
                
                logger.info(f"[DIFY] Failover successful using model: {failover_model}")
                return reply, None
            except Exception as failover_e:
                # 如果故障转移也失败，记录错误并返回原始错误
                logger.exception(f"[DIFY] Failover failed: {failover_e}")
                return None, UNKNOWN_ERROR_MSG

    def _handle_workflow(self, query: str, session: DifySession, context: Context):
        try:
            payload = self._get_workflow_payload(query, session)
            api_key = self._get_dify_conf(context, "dify_api_key", '')
            api_base = self._get_dify_conf(context, "dify_api_base", "https://api.dify.ai/v1")
            dify_client = DifyClient(api_key, api_base)
            response = dify_client._send_request("POST", "/workflows/run", json=payload)
            if response.status_code != 200:
                error_info = f"[DIFY] payload={payload} response text={response.text} status_code={response.status_code}"
                logger.warning(error_info)
                friendly_error_msg = self._handle_error_response(response.text, response.status_code)
                return None, friendly_error_msg

            #  {
            #      "log_id": "djflajgkldjgd",
            #      "task_id": "9da23599-e713-473b-982c-4328d4f5c78a",
            #      "data": {
            #          "id": "fdlsjfjejkghjda",
            #          "workflow_id": "fldjaslkfjlsda",
            #          "status": "succeeded",
            #          "outputs": {
            #          "text": "Nice to meet you."
            #          },
            #          "error": null,
            #          "elapsed_time": 0.875,
            #          "total_tokens": 3562,
            #          "total_steps": 8,
            #          "created_at": 1705407629,
            #          "finished_at": 1727807631
            #      }
            #  }

            rsp_data = response.json()
            if 'data' not in rsp_data or 'outputs' not in rsp_data['data'] or 'text' not in rsp_data['data']['outputs']:
                error_info = f"[DIFY] Unexpected response format: {rsp_data}"
                logger.warning(error_info)
            reply = Reply(ReplyType.TEXT, rsp_data['data']['outputs']['text'])
            return reply, None
        except Exception as e:
            # 记录错误信息
            error_info = f"[DIFY] Exception in _handle_workflow: {e}"
            logger.exception(error_info)
            
            # 使用ChatGPTBot作为故障转移
            try:
                logger.info("[DIFY] Failover to ChatGPTBot")
                from bot.chatgpt.chat_gpt_bot import ChatGPTBot
                
                # 创建ChatGPTBot实例
                failover_bot = ChatGPTBot()
                
                # 使用配置中的failover_model
                failover_model = conf().get("failover_model", "gpt-3.5-turbo")
                
                # 创建新的上下文，正确复制原始context的所有属性
                failover_context = Context(type=ContextType.TEXT)
                
                # 如果原始context存在，复制其content和kwargs
                if context:
                    failover_context.content = context.content
                    for key, value in context.kwargs.items():
                        failover_context[key] = value
                    
                # 设置gpt_model
                failover_context["gpt_model"] = failover_model
                
                # 使用ChatGPTBot处理请求
                reply = failover_bot.reply(query, failover_context)
                
                logger.info(f"[DIFY] Failover successful using model: {failover_model}")
                return reply, None
            except Exception as failover_e:
                # 如果故障转移也失败，记录错误并返回原始错误
                logger.exception(f"[DIFY] Failover failed: {failover_e}")
                return None, UNKNOWN_ERROR_MSG

    def _get_upload_files(self, session: DifySession, context: Context):
        session_id = session.get_session_id()
        img_cache = memory.USER_IMAGE_CACHE.get(session_id)
        if not img_cache or not self._get_dify_conf(context, "image_recognition", False):
            return None
        # 清理图片缓存
        memory.USER_IMAGE_CACHE[session_id] = None
        api_key = self._get_dify_conf(context, "dify_api_key", '')
        api_base = self._get_dify_conf(context, "dify_api_base", "https://api.dify.ai/v1")
        dify_client = DifyClient(api_key, api_base)
        msg = img_cache.get("msg")
        path = img_cache.get("path")
        msg.prepare()

        with open(path, 'rb') as file:
            file_name = os.path.basename(path)
            file_type, _ = mimetypes.guess_type(file_name)
            files = {
                'file': (file_name, file, file_type)
            }
            response = dify_client.file_upload(user=session.get_user(), files=files)

        if response.status_code != 200 and response.status_code != 201:
            error_info = f"[DIFY] response text={response.text} status_code={response.status_code} when upload file"
            logger.warning(error_info)
            return None, error_info
        # {
        #     'id': 'f508165a-10dc-4256-a7be-480301e630e6',
        #     'name': '0.png',
        #     'size': 17023,
        #     'extension': 'png',
        #     'mime_type': 'image/png',
        #     'created_by': '0d501495-cfd4-4dd4-a78b-a15ed4ed77d1',
        #     'created_at': 1722781568
        # }
        file_upload_data = response.json()
        logger.debug("[DIFY] upload file {}".format(file_upload_data))
        return [
            {
                "type": "image",
                "transfer_method": "local_file",
                "upload_file_id": file_upload_data['id']
            }
        ]

    def _fill_file_base_url(self, url: str):
        if url.startswith("https://") or url.startswith("http://"):
            return url
        # 补全文件base url, 默认使用去掉"/v1"的dify api base url
        return self._get_file_base_url() + url

    def _get_file_base_url(self) -> str:
        api_base = conf().get("dify_api_base", "https://api.dify.ai/v1")
        return api_base.replace("/v1", "")

    def _get_workflow_payload(self, query, session: DifySession):
        return {
            'inputs': {
                "query": query
            },
            "response_mode": "blocking",
            "user": session.get_user()
        }

    def _parse_sse_event(self, event_str):
        """
        Parses a single SSE event string and returns a dictionary of its data.
        """
        event_prefix = "data: "
        if not event_str.startswith(event_prefix):
            return None
        trimmed_event_str = event_str[len(event_prefix):]

        # Check if trimmed_event_str is not empty and is a valid JSON string
        if trimmed_event_str:
            try:
                event = json.loads(trimmed_event_str)
                return event
            except json.JSONDecodeError:
                logger.error(f"Failed to decode JSON from SSE event: {trimmed_event_str}")
                return None
        else:
            logger.warning("Received an empty SSE event.")
            return None

    # TODO: 异步返回events
    def _handle_sse_response(self, response: requests.Response):
        events = []
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                event = self._parse_sse_event(decoded_line)
                if event:
                    events.append(event)

        merged_message = []
        accumulated_agent_message = ''
        conversation_id = None
        for event in events:
            event_name = event['event']
            if event_name == 'agent_message' or event_name == 'message':
                accumulated_agent_message += event['answer']
                logger.debug("[DIFY] accumulated_agent_message: {}".format(accumulated_agent_message))
                # 保存conversation_id
                if not conversation_id:
                    conversation_id = event['conversation_id']
            elif event_name == 'agent_thought':
                self._append_agent_message(accumulated_agent_message, merged_message)
                accumulated_agent_message = ''
                logger.debug("[DIFY] agent_thought: {}".format(event))
            elif event_name == 'message_file':
                self._append_agent_message(accumulated_agent_message, merged_message)
                accumulated_agent_message = ''
                self._append_message_file(event, merged_message)
            elif event_name == 'message_replace':
                # TODO: handle message_replace
                pass
            elif event_name == 'error':
                logger.error("[DIFY] error: {}".format(event))
                raise Exception(event)
            elif event_name == 'message_end':
                self._append_agent_message(accumulated_agent_message, merged_message)
                logger.debug("[DIFY] message_end usage: {}".format(event['metadata']['usage']))
                break
            else:
                logger.warning("[DIFY] unknown event: {}".format(event))

        if not conversation_id:
            raise Exception("conversation_id not found")

        return merged_message, conversation_id

    def _append_agent_message(self, accumulated_agent_message,  merged_message):
        if accumulated_agent_message:
            merged_message.append({
                'type': 'agent_message',
                'content': accumulated_agent_message,
            })

    def _append_message_file(self, event: dict, merged_message: list):
        if event.get('type') != 'image':
            logger.warning("[DIFY] unsupported message file type: {}".format(event))
        merged_message.append({
            'type': 'message_file',
            'content': event,
        })

    def _handle_error_response(self, response_text, status_code):
        """处理错误响应并提供用户指导"""
        try:
            friendly_error_msg = UNKNOWN_ERROR_MSG
            error_data = json.loads(response_text)
            if status_code == 400 and "agent chat app does not support blocking mode" in error_data.get("message", "").lower():
                friendly_error_msg = "[DIFY] 请把config.json中的dify_app_type修改为agent再重启机器人尝试"
                print_red(friendly_error_msg)
            elif status_code == 401 and error_data.get("code").lower() == "unauthorized":
                friendly_error_msg = "[DIFY] apikey无效, 请检查config.json中的dify_api_key或dify_api_base是否正确"
                print_red(friendly_error_msg)
            return friendly_error_msg
        except Exception as e:
            logger.error(f"Failed to handle error response, response_text: {response_text} error: {e}")
            return UNKNOWN_ERROR_MSG
