# -*- coding: utf-8 -*-
import re
import os
import traceback
import sys
from pathlib import Path
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
import __main__  # noqa
import uvicorn
import socket
import requests
import time
import argparse
import concurrent.futures
from loguru import logger
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHANGELOG_PATH = os.path.join(BASE_DIR, "txt", "CHANGELOG.md")


class ResponseCode:
    """响应状态码配置"""
    SUCCESS = (0, "success")
    ERROR = (300, "error")
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
    def run_server(default_port: int = 8000, require_inner_url: bool = False, require_library: bool = False):
        """
        Uvicorn 启动入口，支持跨平台与容器场景。
        :param default_port: 默认端口号
        :param require_inner_url: 是否强依赖内部接口地址
        :param require_library: 是否强依赖库接口
        """
        parser = argparse.ArgumentParser(description=f"API Service")
        parser.add_argument("-j", "--project", type=str, default=None, help="项目名称")
        parser.add_argument("-H", "--host", type=str, default="0.0.0.0", help="绑定地址 (默认: 0.0.0.0)")
        parser.add_argument("-p", "--port", type=int, default=default_port, help=f"启动端口 (默认: {default_port})")
        parser.add_argument("-w", "--workers", type=int, default=1, help="工作进程数 (默认: 1)")
        parser.add_argument("-a", "--app", type=str, default=None, help="ASGI 入口，例如 chemparse_server:app")
        parser.add_argument("-L", "--log-level", type=str, default="info", help="日志级别 (默认: info)")
        parser.add_argument(
            "--limit-max-requests",
            type=int,
            default=0,
            help="每个 worker 最多处理请求数，达到后自动重启 (0 表示不限制)"
        )
        parser.add_argument(
            "--timeout-worker-healthcheck",
            type=int,
            default=10,
            help="worker 健康检查超时秒数 (默认: 10)"
        )
        parser.add_argument(
            "-i",
            "--inner_url",
            type=str,
            default=None,
            required=require_inner_url,
            help="内部接口地址" + (" (必填)" if require_inner_url else " (默认: None)")
        )
        parser.add_argument(
            "-l",
            "--library",
            type=str,
            default=None,
            required=require_library,
            help="数据库依赖" + (" (必填)" if require_library else " (默认: None)")
        )
        parser.add_argument("-U", "--username", type=str, default=None, help="数据库访问账号")
        parser.add_argument("-P", "--password", type=str, default=None, help="数据库访问密码")

        args = parser.parse_args()

        host = args.host
        port = args.port
        workers = ServerUtil._normalize_workers(args.workers)
        if args.project:
            project_name = args.project
            os.environ["PROJECT_NAME"] = project_name
        else:
            project_name = "Unknown"
        if args.inner_url:
            os.environ["SERVICE_INNER_URL"] = args.inner_url
        if args.library:
            os.environ["SERVICE_LIBRARY_URL"] = args.library
        if args.username:
            os.environ["DB_USERNAME"] = args.username
        if args.password:
            os.environ["DB_PASSWORD"] = args.password

        if workers != args.workers:
            logger.warning(f"workers 参数非法({args.workers})，已自动调整为 {workers}")

        if workers > 1 and ServerUtil._is_linux_container():
            cpu_count = os.cpu_count() or 1
            recommended_workers = max(1, min(cpu_count, 4))
            if workers > recommended_workers:
                logger.warning(
                    f"检测到 Linux 容器环境，当前 workers={workers} 偏高，建议 <= {recommended_workers}，"
                    f"否则可能出现 OOM 或 'Child process died'。"
                )

        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa
            local_ip = "127.0.0.1"

        app_target, app_dir = ServerUtil._resolve_app_target(args.app)
        if app_dir and app_dir not in sys.path:
            sys.path.insert(0, app_dir)

        logger.info(
            f"\n项目名称: {project_name}\n"
            f"局域网访问: http://{local_ip}:{port}\n"
            f"Swagger文档: http://{local_ip}:{port}/docs\n"
            f"配置参数: host={host}, workers={workers}, app={app_target}, app_dir={app_dir}\n"
            f"内部接口地址: {args.inner_url}\n"
            f"外部库地址: {args.library}"
        )

        uvicorn.run(
            app_target,
            host=host,
            port=port,
            workers=workers,
            app_dir=app_dir,
            log_level=args.log_level,
            timeout_worker_healthcheck=args.timeout_worker_healthcheck,
            limit_max_requests=args.limit_max_requests if args.limit_max_requests > 0 else None,
            reload=False,
        )

    @staticmethod
    async def unified_exception_handler(request: FastAPIRequest, exc: Exception):
        """
        具体的异常处理逻辑
        """
        if isinstance(exc, RequestValidationError):
            logger.error(f"【参数校验拦截】 URL: {request.url} \n{str(exc)}")
        else:
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
