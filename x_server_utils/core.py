# -*- coding: utf-8 -*-
import re
import os
import traceback
import sys
from pathlib import Path

import dirtyjson
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.exceptions import RequestValidationError, HTTPException
from starlette.responses import JSONResponse
import __main__  # noqa
import uvicorn
import socket
import requests
import time
import argparse
import concurrent.futures
import base64
import io
import json
import openai
from PIL import Image
from loguru import logger

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHANGELOG_PATH = os.path.join(BASE_DIR, "txt", "CHANGELOG.md")
PROJECT_CONFIG = {
    'osra': {
        'port': 2089,
        'required_params': [],
    },
    'fileparse': {
        'port': 2091,
        'required_params': ['inner_url', 'library'],
    },
    'chemparse': {
        'port': 2088,
        'required_params': ['inner_url', 'library'],
    },
    'patenthtml': {
        'port': 2090,
        'required_params': [],
    },
    'all': {
        'port': 7000,
        'required_params': ['inner_url', 'library'],
    },
}
ENV_MAPPING = [
    ('project', 'PROJECT_NAME'),
    ('inner_url', 'SERVICE_INNER_URL'),
    ('library', 'SERVICE_LIBRARY_URL'),
    ('username', 'DB_USERNAME'),
    ('password', 'DB_PASSWORD'),
]


class ResponseCode:
    """响应状态码配置"""
    SUCCESS = (0, "success")
    SUCCESS_BUT_EMPTY = (0, "success, molecular structure is empty")
    ERROR = (300, "error")
    # 310-329 为解析失败，status=5，可重新解析
    PARSING_FAILED = (310, "parsing failed")
    # PARSING_EMPTY = (320, "parsing is empty")
    # 330-360 为文件错误，status=7，不重新解析
    FILE_ERROR = (330, "file error, unable to parse")
    FILE_EMPTY = (335, "file is empty")
    # PARSING_EMPTY = (336, "parsing is empty")
    FILE_TYPE_UNSUPPORTED = (340, "unsupported file types")
    FILE_LANG_UNSUPPORTED = (350, "unsupported file language")
    TIMEOUT_ALREADY = (355, "already timeout, task failed")
    TIMEOUT = (360, "timeout 600s")
    # 常规错误码
    FAIL = (400, "fail")
    UNAUTHORIZED = (401, "unauthorized")
    NOT_FOUND = (404, "not found")
    EXCEED_TIME = (408, "request timeout")
    INNER_ERROR = (500, "internal server error")
    ROBOT_VERIFY = (1001, "robot verify required")


class ParseStatus:
    """解析状态枚举"""
    UPLOADING = (1, "上传中")
    UPLOAD_COMPLETED = (2, "上传完成")
    PARSING = (3, "解析中")
    PARSING_COMPLETED = (4, "解析完成")
    PARSING_FAILED = (5, "解析失败")
    UPLOAD_FAILED = (6, "上传失败")
    FILE_ERROR = (7, "文件错误，无法解析")


class ServerUtil(object):
    @staticmethod
    def _resolve_app_target(explicit_app: str | None = None):
        """
        Resolve ASGI app import target and app_dir for uvicorn.
        Returns:
            tuple[str, str | None]: ("module:app", app_dir)
        """
        if explicit_app:
            explicit_app = explicit_app.strip()
            if ":" not in explicit_app:
                raise ValueError("--app 参数格式错误，需为 'module:app'")
            return explicit_app, None

        app_dir = None
        module_name = None

        main_spec = getattr(__main__, "__spec__", None)
        if main_spec and getattr(main_spec, "name", None) and main_spec.name != "__main__":
            module_name = main_spec.name

        main_file = getattr(__main__, "__file__", None)
        if main_file:
            main_path = Path(main_file).resolve()
            app_dir = str(main_path.parent)
            if not module_name:
                module_name = main_path.stem

        if not module_name:
            raise RuntimeError("无法自动解析启动模块，请通过 --app 显式指定，例如 --app chemparse_server:app")

        return f"{module_name}:app", app_dir

    @staticmethod
    def _normalize_workers(workers: int):
        """Ensure workers is always >= 1."""
        if workers is None:
            return 1
        return max(1, workers)

    @staticmethod
    def _is_linux_container():
        """Best-effort check for Linux container environments."""
        if os.name == "nt":
            return False
        if os.path.exists("/.dockerenv"):
            return True
        cgroup_path = "/proc/1/cgroup"
        if os.path.exists(cgroup_path):
            try:
                with open(cgroup_path, "r", encoding="utf-8") as f:
                    cgroup_data = f.read()
                if "docker" in cgroup_data or "kubepods" in cgroup_data or "containerd" in cgroup_data:
                    return True
            except Exception:  # noqa
                pass
        return False

    @staticmethod
    def get_server_description():
        """
        读取描述文件和更新日志，并提取最新版本号
        :return: (changelog_content: str, latest_version: str)
        """
        changelog_content = ""
        latest_version = "未知版本"  # 默认值，防止没匹配到时报错
        try:
            if os.path.exists(CHANGELOG_PATH):
                with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
                    changelog_content = f.read()
                match = re.search(r"#{2,4}\s*\[([^]]+)]", changelog_content)
                if match:
                    latest_version = match.group(1)  # 提取括号里面的内容，例如 1.4.0
        except Exception as e:
            logger.warning(f"读取更新日志失败: {e}")
        return changelog_content, latest_version

    @staticmethod
    def run_server(default_port: int = 7000):
        """
        Uvicorn 启动入口，支持跨平台与容器场景。
        :param default_port: 默认端口号
        """
        parser = argparse.ArgumentParser(description="API Service")
        parser.add_argument("-j", "--project", type=str, default="all", help="项目名称")
        parser.add_argument("-H", "--host", type=str, default="0.0.0.0", help="绑定地址 (默认: 0.0.0.0)")
        parser.add_argument("-p", "--port", type=int, default=default_port, help=f"启动端口 (默认: {default_port})")
        parser.add_argument("-w", "--workers", type=int, default=1, help="工作进程数 (默认: 1)")
        parser.add_argument("-a", "--app", type=str, default=None, help="ASGI 入口，例如 chemparse_server:app")
        parser.add_argument("-L", "--log-level", type=str, default="info", help="日志级别 (默认: info)")
        parser.add_argument("--limit-max-requests", type=int, default=0,
                            help="每个 worker 最多处理请求数，达到后自动重启 (0 表示不限制)")
        parser.add_argument("--timeout-worker-healthcheck", type=int, default=10,
                            help="worker 健康检查超时秒数 (默认: 10)")
        parser.add_argument("-i", "--inner_url", type=str, default=None, help="内部接口地址")
        parser.add_argument("-l", "--library", type=str, default=None, help="数据库依赖")
        parser.add_argument("-U", "--username", type=str, default=None, help="数据库访问账号")
        parser.add_argument("-P", "--password", type=str, default=None, help="数据库访问密码")

        args = parser.parse_args()

        # 参数校验
        project_name = args.project
        if project_name not in PROJECT_CONFIG:
            logger.warning(f'非预设项目: {project_name}，请确认项目配置是否正确，可选值: {list(PROJECT_CONFIG.keys())}')

        project_config = PROJECT_CONFIG.get(project_name, {})

        # 端口冲突检测
        if args.port == default_port and project_config.get('port', default_port) != default_port:
            logger.warning(f"项目 {project_name} 推荐使用端口 {project_config.get('port')}，当前使用 {args.port}")

        # 必需参数校验
        missing_params = []
        for param in project_config.get('required_params', []):
            if not getattr(args, param, None):  # 使用默认值 None 避免属性不存在错误
                missing_params.append(param)

        if missing_params:
            raise ValueError(f"项目 {project_name} 缺少必需参数: {', '.join(missing_params)}")

        # 设置环境变量（使用统一方法）
        for arg_name, env_name in ENV_MAPPING:
            value = getattr(args, arg_name, None)
            if value:
                os.environ[env_name] = value

        # 工作进程数规范化
        workers = ServerUtil._normalize_workers(args.workers)
        if workers != args.workers:
            logger.warning(f"workers 参数非法({args.workers})，已自动调整为 {workers}")

        # 容器环境检查
        if workers > 1 and ServerUtil._is_linux_container():
            cpu_count = os.cpu_count() or 1
            recommended_workers = max(1, min(cpu_count, 4))
            if workers > recommended_workers:
                logger.warning(
                    f"检测到 Linux 容器环境，当前 workers={workers} 偏高，建议 <= {recommended_workers}，"
                    f"否则可能出现 OOM 或 'Child process died'。"
                )

        # 获取本地IP
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except:  # noqa
            local_ip = "127.0.0.1"

        # 解析应用目标
        app_target, app_dir = ServerUtil._resolve_app_target(args.app)
        if app_dir and app_dir not in sys.path:
            sys.path.insert(0, app_dir)

        # 启动信息日志
        logger.info(
            f"\n{'=' * 50}\n"
            f"项目名称: {project_name}\n"
            f"局域网访问: http://{local_ip}:{args.port}\n"
            f"Swagger文档: http://{local_ip}:{args.port}/docs\n"
            f"配置参数:\n"
            f"  - 绑定地址: {args.host}\n"
            f"  - 工作进程: {workers}\n"
            f"  - 应用入口: {app_target}\n"
            f"  - 应用目录: {app_dir}\n"
            f"  - 日志级别: {args.log_level}\n"
            f"  - 内部接口: {args.inner_url or '未配置'}\n"
            f"  - 外部库: {args.library or '未配置'}\n"
            f"{'=' * 50}"
        )

        # 启动服务
        try:
            uvicorn.run(
                app_target,
                host=args.host,
                port=args.port,
                workers=workers,
                app_dir=app_dir,
                log_level=args.log_level,
                timeout_worker_healthcheck=args.timeout_worker_healthcheck,
                limit_max_requests=args.limit_max_requests if args.limit_max_requests > 0 else None,
                reload=False,
            )
        except Exception as e:
            logger.error(f"服务启动失败: {str(e)}")
            raise

    @staticmethod
    async def unified_exception_handler(request: FastAPIRequest, exc: Exception):
        """
        具体的异常处理逻辑
        """
        if isinstance(exc, RequestValidationError):
            # 参数校验错误 - 应该返回400或422，并提供具体的错误信息
            logger.error(f"【参数校验拦截】 URL: {request.url} \n{str(exc)}")
            errors = exc.errors()
            return JSONResponse(
                status_code=400,  # 通常参数错误使用422 Unprocessable Entity
                content={
                    'code': 400,  # 可以定义专门的参数错误码
                    'data': [],
                    'message': f"请求参数校验失败, {errors}"
                }
            )
        elif isinstance(exc, HTTPException):
            # HTTP异常处理
            logger.error(f"【HTTP异常拦截】 URL: {request.url} \n{str(exc)}")
            return JSONResponse(
                status_code=400,
                content={
                    'code': 400,
                    'data': [],
                    'message': f'HTTP异常 {exc.detail}'
                }
            )
        else:
            # 其他未预期的异常 - 返回500
            logger.error(f"【全局代码异常拦截】 URL: {request.url} \n{traceback.format_exc()}")
            status = ResponseCode.INNER_ERROR
            return JSONResponse(
                status_code=500,
                content={
                    'code': status[0],
                    'data': [],
                    'message': status[1]
                }
            )

    @staticmethod
    def register_global_exceptions(app: FastAPI):
        """
        暴露给外部的注册函数：将拦截器绑定到传入的 FastAPI 实例上
        """
        # 相当于 @app.exception_handler(RequestValidationError)
        app.add_exception_handler(RequestValidationError, ServerUtil.unified_exception_handler)
        # 相当于 @app.exception_handler(Exception)
        app.add_exception_handler(Exception, ServerUtil.unified_exception_handler)

    @staticmethod
    def register_global_middlewares(app: FastAPI):
        """
        注册全局中间件（如：请求/响应耗时与日志追踪）
        """

        @app.middleware("http")
        async def log_request_response(request: FastAPIRequest, call_next):
            client_ip = request.client.host if request.client else "Unknown"
            request_id = int(time.time() * 1000)  # 简单的请求链路ID，方便在并发时匹配日志

            logger.info(
                f"[Req:{request_id}] 收到请求 | 来源IP: {client_ip} | 路径: {request.method} {request.url.path}")

            start_time = time.time()
            try:
                # 放行请求给后续路由
                response = await call_next(request)

                process_time = time.time() - start_time
                logger.info(f"[Req:{request_id}] 发送响应 | 状态码: {response.status_code} | 耗时: {process_time:.3f}s")
                return response

            except Exception as e:
                # 注意：业务抛出的异常其实会被 register_global_exceptions 提前捕获并转为 500 状态码
                # 只有发生框架级/底层异常时，才会走到这里
                process_time = time.time() - start_time
                logger.error(f"[Req:{request_id}] 响应异常 | 耗时: {process_time:.3f}s | 异常信息: {str(e)}")
                raise e


class ModelClient(object):
    def __init__(self, user_config: dict, cost_config: dict, prompt_config: dict):
        self.user_config = user_config
        self.cost_config = cost_config
        self.prompt_config = prompt_config
        self.client = self.connect_init()

    def connect_init(self):
        return openai.OpenAI(api_key=self.user_config['api_key'], base_url=self.user_config['base_url'])

    def generate_content(self, task_name: str, user_input: str | list | dict, model_id: str = None,
                         timeout: int = 300, max_retries: int = 2, mode: str = 'formal'):
        start_time = time.time()
        llm_config = self.prompt_config[task_name]
        sys_prompt = llm_config['prompt']
        task_type = llm_config['task_type']

        if task_type in ['image', 'doc', 'multi']:
            # 1. 设置系统提示词
            messages = [{"role": "system", "content": sys_prompt}]
            user_content = []
            # 2. 判断 user_input 是否为包含了文本和图片的多模态字典结构
            if isinstance(user_input, dict):
                # 提取并添加结构化文本
                parsed_text = user_input.get("text")
                if parsed_text:
                    user_content.append({"type": "text", "text": parsed_text})
                # 提取图片列表
                images = user_input.get("doc") or user_input.get("image")
                if not isinstance(images, list):
                    images = [images]
            # 3. 兼容旧逻辑：如果直接传入的是 list 或 str，默认全是图片
            elif isinstance(user_input, str):
                images = [user_input]
            else:
                images = user_input

            # 4. 遍历添加图片
            mime_type = llm_config.get('mime_type', 'image/png')
            for b64 in images:
                user_content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})
            # 5. 组合 User Message
            messages.append({"role": "user", "content": user_content})

        elif task_type == 'text':
            messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_input}]

        else:
            logger.error(f"不支持的任务类型: {task_type}")
            return None, None

        if not self.client:
            self.client = self.connect_init()

        model_id = model_id or llm_config['model_id']
        for attempt in range(max_retries):
            try:
                if mode == 'test':
                    payload = {
                        "contents": [
                            {"role": "model", "parts": [{"text": sys_prompt}]},
                            {"role": "user", "parts": [{"text": user_input}]}
                        ],
                        "generationConfig": {
                            "temperature": llm_config['temperature'], "maxOutputTokens": llm_config['maxOutputTokens'],
                            "topP": llm_config['topP'], "frequencyPenalty": 0.0, "responseMimeType": "application/json"
                        }
                    }
                    response = requests.post(url=self.user_config['temp_url'], json=payload, timeout=timeout)
                    response = response.json()
                    finish_reason = None
                    if response:
                        try:
                            finish_reason = response.get('candidates')[0].get('finishReason').lower()
                        except:  # noqa
                            pass
                    model_name = 'gemini'

                else:
                    response = self.client.chat.completions.create(
                        model=model_id,
                        messages=messages,
                        temperature=llm_config['temperature'],
                        max_tokens=llm_config['maxOutputTokens'],
                        max_completion_tokens=llm_config.get('maxOutputTokens', 4096),  # 兼容新旧 API
                        top_p=llm_config['topP'],
                        frequency_penalty=llm_config.get('frequencyPenalty'),
                        response_format=llm_config.get('response_format'),
                        timeout=timeout,
                        reasoning_effort=llm_config.get('thinkingLevel')
                    )
                    finish_reason = response.choices[0].finish_reason
                    model_name = 'openai'

                if finish_reason == 'stop':
                    result = ModelClient.parse_model_response(response, model_name, model_id)
                    cost = self.record_token_cost(response, model_id, task_name, finish_reason, start_time, mode)
                    return result, cost
                else:
                    logger.error(f"大模型吞吐异常，任务: {task_name}, 完成原因: {finish_reason}, 响应: {response}")

            except Exception as e:
                logger.error(f"模型 {model_id} 任务 {task_name} response_format {llm_config['response_format']} "
                             f"输入内容 {str(user_input)[:200]} 调用失败: {str(e)}\n{traceback.format_exc()}")

            if attempt < max_retries - 1:
                logger.warning(f"模型 {model_id} 任务: {task_name} 解析失败，重试 {attempt + 1}/{max_retries - 1} ...")

        spend_time = round(time.time() - start_time, 2)
        logger.error(f"达到最大重试次数，放弃任务: {task_name}, 耗时: {spend_time}")
        return None, None

    @staticmethod
    def parse_model_response(raw_data, model_name: str, model_id: str) -> dict | list | str:
        """
        解析大模型返回的文本，提取有效的 JSON 数据或原始文本。

        Args:
            raw_data: 大模型返回的原始数据。
            model_name: 模型名称。
            model_id: 模型 ID。

        Returns:
            dict | list | str: 解析后的 JSON 数据（字典或列表），失败时返回原始文本。
        """

        def _extract_response_text(model: str, data) -> str:
            """从不同模型的响应结构中提取文本内容"""
            if model == 'openai':
                return data.choices[0].message.content

            elif model == 'gemini':
                return data['candidates'][0]['content']['parts'][0]['text']
                # candidate = data.get('candidates', [{}])[0]
                # return candidate.get('content', {}).get('parts', [{}])[0].get('text', '')

            elif model in ('gpt', 'doubao'):
                choice = data.get('choices', [{}])[0]
                return choice.get('message', {}).get('content', '')

            else:
                logger.error(f"不支持的模型名称: {model}")
                return ''

        def _preprocess_text(text_ori: str) -> str:
            """移除 JSON 标记和前后空白"""
            text_ori = text_ori.strip()
            return re.sub(r'^```(json)?|```$', '', text_ori, flags=re.IGNORECASE).strip()

        def _try_parse_json(text_ori: str) -> dict | list | None:
            """尝试多种方式解析 JSON"""
            # 准备工作：如果是空的字符串直接返回
            if not text_ori or not any(c in text_ori for c in '{['):
                return None

            # 尝试 1：直接解析
            try:
                return json.loads(text_ori)
            except json.JSONDecodeError:
                pass

            # 尝试 2：使用 dirtyjson 解析 (强力兜底)
            # 它能处理：末尾多余符号、缺少括号、单引号、非转义字符等
            try:
                result = dirtyjson.loads(text_ori)
                # dirtyjson 返回的是 AttributedDict 或 AttributedList
                # 转换为标准 dict/list 以保持程序一致性
                if isinstance(result, (dict, list)):
                    # 通过重新 dump/load 或者递归转换确保类型纯净
                    # 这里推荐直接返回，因为 AttributedDict 兼容 dict 接口
                    # json.loads(json.dumps(result))
                    logger.success(f"dirtyjson 解析成功: {json.dumps(result)[:200]} ...")
                    return result
            except Exception as e:
                logger.debug(f"dirtyjson 解析失败: {e}")
                pass

            # 尝试 3：处理多行 JSON 或片段
            if '\n' in text_ori:
                normalized_text = re.sub(r'}\s*{', '},{', text_ori)
                if not normalized_text.startswith('[') and '{' in normalized_text:
                    normalized_text = f'[{normalized_text}]'
                try:
                    return json.loads(normalized_text)
                except json.JSONDecodeError:
                    pass

            # 尝试 4：提取最外层 {} 或 [] 包裹的内容
            for wrapper in ('{}', '[]'):
                try:
                    if wrapper[0] in text_ori and wrapper[-1] in text_ori:
                        start = text_ori.find(wrapper[0])
                        end = text_ori.rfind(wrapper[-1]) + 1
                        if start < end:
                            substring = text_ori[start:end]
                            # 对提取出的子串再次尝试标准解析和 dirty 采样
                            try:
                                return json.loads(substring)
                            except:  # noqa
                                return dirtyjson.loads(substring)
                except Exception:  # noqa
                    continue

            return None

        # 1. 提取模型响应中的文本内容
        text = _extract_response_text(model_name, raw_data)
        if not text:
            logger.warning(f"模型 {model_id} 的响应中未找到有效文本内容")
            return text

        # 2. 预处理文本（移除 JSON 标记和空白）
        text = _preprocess_text(text)

        # 3. 尝试解析为 JSON
        parsed_data = _try_parse_json(text)
        if parsed_data is not None:
            return parsed_data

        logger.warning("无法解析为有效 JSON，返回原始文本")
        return text

    def record_token_cost(self, llm_response, model_id: str, task_name: str,
                          finish_reason: str, start_time: float, mode: str = None) -> dict:
        """
        记录Gemini API调用的token消耗和成本。
        Args:
            llm_response: 包含API调用的结果。
            model_id: 模型 ID。
            task_name: 任务名称。
            finish_reason: 完成原因。
            start_time: 开始时间戳。
            mode: 记录模式，'formal' 或 'test'。
        Returns:
            dict: 更新后的结果字典，包含新增的'tokenCost'键，值为计算出的成本（单位：美元）。
        """
        try:
            metrics = llm_response.metrics
        except:  # noqa
            try:
                usage = llm_response.usage
                metrics = {
                    'input_token_count': usage.prompt_tokens,
                    'output_token_count': usage.completion_tokens,
                }
            except:  # noqa
                metrics = None

        usage_record = {}
        if metrics:
            model_cost_info = self.cost_config[model_id]
            input_token_count = metrics.get('input_token_count', 0)
            output_token_count = metrics.get('output_token_count', 0)
            input_cost = model_cost_info['input'] / 1000000 * input_token_count
            output_cost = model_cost_info['output'] / 1000000 * output_token_count
            total_cost = (input_cost + output_cost) * self.cost_config['usd_to_cny']
            usage_record = {
                "task_name": task_name,
                "input_token": input_token_count,
                "output_token": output_token_count,
                "cost": round(total_cost, 8),
            }
        else:
            try:
                usage_record = self.record_token_cost_gemini_style(llm_response, model_id, task_name, finish_reason)
            except Exception as e:
                logger.warning(f"任务: {task_name}, 模型: {model_id} 解析token信息失败，{e}\n{traceback.format_exc()}")

        preview = ModelClient.format_response_preview(llm_response)
        spend_time = round(time.time() - start_time, 2)
        usage_record['spend_time'] = spend_time
        logger.info(
            f"任务: {task_name}, 模型: {model_id}, 输出: {preview}, "
            f"完成原因: {finish_reason}, 消耗: {usage_record.get('cost', 0):.4f}元, 耗时：{spend_time}秒, 模式: {mode}")
        return usage_record

    def record_token_cost_gemini_style(self, response, model_id: str, task_name: str,
                                       finish_reason: str) -> dict:
        """简化版 Gemini Token 计费逻辑"""
        meta = response.get('usageMetadata', {})
        cfg = self.cost_config.get(model_id, {})
        usd_to_cny = self.cost_config.get('usd_to_cny', 6.88)

        # 辅助函数：优先从明细提取，否则取总数
        def get_tokens(details, total_key, modalities=None):
            if modalities is None:
                modalities = ['TEXT', 'IMAGE', 'DOCUMENT']
            if details:
                return sum(d.get('tokenCount', 0) for d in details if d.get('modality') in modalities)
            return meta.get(total_key, 0)

        # 1. 计算输入
        in_tokens = get_tokens(meta.get('promptTokensDetails'), 'promptTokenCount')
        in_cost = in_tokens * (cfg.get('input', 0) / 1e6)

        # 2. 计算输出
        c_details = meta.get('candidatesTokensDetails', [])
        thoughts = meta.get('thoughtsTokenCount', 0)

        if model_id == 'gemini-2.5-flash-image-preview' and c_details:
            # 特殊逻辑：图像按张数/等效Token计费，文本(含思考)按量计费
            img_tk = get_tokens(c_details, '', ['IMAGE'])
            txt_tk = get_tokens(c_details, '', ['TEXT']) + thoughts
            img_cost = (img_tk / cfg.get('avg_image_cost', 1)) * cfg.get('image_output', 0)
            out_cost = img_cost + (txt_tk * (cfg.get('output', 0) / 1e6))
            out_tokens = img_tk + txt_tk
        else:
            # 通用逻辑
            out_tokens = get_tokens(c_details, 'candidatesTokenCount') + thoughts
            out_cost = out_tokens * (cfg.get('output', 0) / 1e6)

        # 3. 汇总结果
        total_cost_cny = round((in_cost + out_cost) * usd_to_cny, 8)
        res = {
            "task_name": task_name,
            "input_token": in_tokens,
            "output_token": out_tokens,
            "cost": total_cost_cny
        }

        logger.info(f"GE解析[{task_name}] 消耗: {in_tokens}in/{out_tokens}out, "
                    f"花费: ¥{total_cost_cny:.4f}, 完成原因: {finish_reason}")
        return res

    @staticmethod
    def format_response_preview(response, max_preview=200):
        """格式化响应内容预览

        Args:
            response: 响应内容
            max_preview: 前后预览的最大字符数

        Returns:
            格式化后的预览字符串
        """
        response_str = str(response)
        total_len = len(response_str)

        if total_len == 0:
            return "[空响应]"
        elif total_len <= max_preview:
            return response_str
        elif total_len <= max_preview * 2:
            return f"{response_str[:max_preview]} ... (后{total_len - max_preview}字符省略)"
        else:
            return (f"{response_str[:max_preview]} ... {response_str[-max_preview:]} "
                    f"(总长度: {total_len}字符, 中间{total_len - max_preview * 2}字符省略)")

    @staticmethod
    def binary_to_base64(binary_data):
        img_byte_arr = io.BytesIO()
        Image.open(io.BytesIO(binary_data)).convert('RGB').save(img_byte_arr, format='PNG')
        return base64.b64encode(img_byte_arr.getvalue()).decode("ascii")


class StressTester(object):
    @staticmethod
    def send_request(url, img_data):
        """发送单次请求并统计时间"""
        start_time = time.time()
        try:
            payload = {'image_base64': img_data}
            response = requests.post(url, json=payload, timeout=40)
            duration = time.time() - start_time

            if response.status_code == 200:
                return True, duration
            else:
                return False, duration
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"请求异常: {e}")
            return False, duration

    @staticmethod
    def run_stress_test(img_data, url, workers, total_requests):
        # 准备图片数据
        logger.info(f"开始压测: URL={url}, 并发数={workers}, 总请求数={total_requests}")
        results = []
        start_wall_time = time.time()

        # 使用线程池模拟并发
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            # 提交所有任务
            futures = [executor.submit(StressTester.send_request, url, img_data) for _ in range(total_requests)]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

        end_wall_time = time.time()
        total_wall_time = end_wall_time - start_wall_time

        # 统计数据
        success_count = sum(1 for r in results if r[0])
        fail_count = total_requests - success_count
        durations = [r[1] for r in results]

        avg_time = sum(durations) / len(durations) if durations else 0
        qps = total_requests / total_wall_time

        print("\n" + "=" * 50)
        print("压测结果报告")
        print("=" * 50)
        print(f"并发数 (Workers):    {workers}")
        print(f"总请求数:            {total_requests}")
        print(f"成功次数:            {success_count}")
        print(f"失败次数:            {fail_count}")
        print(f"总耗时:              {total_wall_time:.2f} 秒")
        print(f"每秒请求数 (QPS):    {qps:.2f}")
        print(f"平均响应时间:        {avg_time * 1000:.2f} 毫秒")
        print(f"最快响应时间:        {min(durations) * 1000:.2f} 毫秒")
        print(f"最慢响应时间:        {max(durations) * 1000:.2f} 毫秒")
        print("=" * 50)
