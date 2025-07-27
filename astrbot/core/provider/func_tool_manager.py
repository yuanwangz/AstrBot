from __future__ import annotations
import json
import textwrap
import os
import asyncio
import logging
from datetime import timedelta

from typing import Dict, List, Awaitable, Literal, Any
from dataclasses import dataclass
from typing import Optional
from contextlib import AsyncExitStack
from astrbot import logger
from astrbot.core.utils.log_pipe import LogPipe

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    import mcp
    from mcp.client.sse import sse_client
except (ModuleNotFoundError, ImportError):
    logger.warning("警告: 缺少依赖库 'mcp'，将无法使用 MCP 服务。")

try:
    from mcp.client.streamable_http import streamablehttp_client
except (ModuleNotFoundError, ImportError):
    logger.warning(
        "警告: 缺少依赖库 'mcp' 或者 mcp 库版本过低，无法使用 Streamable HTTP 连接方式。"
    )

DEFAULT_MCP_CONFIG = {"mcpServers": {}}

SUPPORTED_TYPES = [
    "string",
    "number",
    "object",
    "array",
    "boolean",
]  # json schema 支持的数据类型


def _prepare_config(config: dict) -> dict:
    """准备配置，处理嵌套格式"""
    if "mcpServers" in config and config["mcpServers"]:
        first_key = next(iter(config["mcpServers"]))
        config = config["mcpServers"][first_key]
    config.pop("active", None)
    return config


async def _quick_test_mcp_connection(config: dict) -> tuple[bool, str]:
    """快速测试 MCP 服务器可达性"""
    import aiohttp

    cfg = _prepare_config(config.copy())

    url = cfg["url"]
    headers = cfg.get("headers", {})
    timeout = cfg.get("timeout", 10)

    try:
        async with aiohttp.ClientSession() as session:
            if cfg.get("transport") == "streamable_http":
                test_payload = {
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "id": 0,
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1.2.3"},
                    },
                }
                async with session.post(
                    url,
                    headers={
                        **headers,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    json=test_payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if response.status == 200:
                        return True, ""
                    else:
                        return False, f"HTTP {response.status}: {response.reason}"
            else:
                async with session.get(
                    url,
                    headers={
                        **headers,
                        "Accept": "application/json, text/event-stream",
                    },
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    if response.status == 200:
                        return True, ""
                    else:
                        return False, f"HTTP {response.status}: {response.reason}"

    except asyncio.TimeoutError:
        return False, f"连接超时: {timeout}秒"
    except Exception as e:
        return False, f"{e!s}"


@dataclass
class FuncTool:
    """
    用于描述一个函数调用工具。
    """

    name: str
    parameters: Dict
    description: str
    handler: Awaitable = None
    """处理函数, 当 origin 为 mcp 时，这个为空"""
    handler_module_path: str = None
    """处理函数的模块路径，当 origin 为 mcp 时，这个为空

    必须要保留这个字段, handler 在初始化会被 functools.partial 包装，导致 handler 的 __module__ 为 functools
    """
    active: bool = True
    """是否激活"""

    origin: Literal["local", "mcp"] = "local"
    """函数工具的来源, local 为本地函数工具, mcp 为 MCP 服务"""

    # MCP 相关字段
    mcp_server_name: str = None
    """MCP 服务名称，当 origin 为 mcp 时有效"""
    mcp_client: MCPClient = None
    """MCP 客户端，当 origin 为 mcp 时有效"""

    def __repr__(self):
        return f"FuncTool(name={self.name}, parameters={self.parameters}, description={self.description}, active={self.active}, origin={self.origin})"

    async def execute(self, **args) -> Any:
        """执行函数调用"""
        if self.origin == "local":
            if not self.handler:
                raise Exception(f"Local function {self.name} has no handler")
            return await self.handler(**args)
        elif self.origin == "mcp":
            if not self.mcp_client or not self.mcp_client.session:
                raise Exception(f"MCP client for {self.name} is not available")
            # 使用name属性而不是额外的mcp_tool_name
            actual_tool_name = (
                self.name.split(":")[-1] if ":" in self.name else self.name
            )
            return await self.mcp_client.session.call_tool(actual_tool_name, args)
        else:
            raise Exception(f"Unknown function origin: {self.origin}")


class MCPClient:
    def __init__(self):
        # Initialize session and client objects
        self.session: Optional[mcp.ClientSession] = None
        self.exit_stack = AsyncExitStack()

        self.name = None
        self.active: bool = True
        self.tools: List[mcp.Tool] = []
        self.server_errlogs: List[str] = []
        self.running_event = asyncio.Event()

    async def connect_to_server(self, mcp_server_config: dict, name: str):
        """连接到 MCP 服务器

        如果 `url` 参数存在：
            1. 当 transport 指定为 `streamable_http` 时，使用 Streamable HTTP 连接方式。
            1. 当 transport 指定为 `sse` 时，使用 SSE 连接方式。
            2. 如果没有指定，默认使用 SSE 的方式连接到 MCP 服务。

        Args:
            mcp_server_config (dict): Configuration for the MCP server. See https://modelcontextprotocol.io/quickstart/server
        """
        cfg = _prepare_config(mcp_server_config.copy())

        def logging_callback(msg: str):
            # 处理 MCP 服务的错误日志
            print(f"MCP Server {name} Error: {msg}")
            self.server_errlogs.append(msg)

        if "url" in cfg:
            success, error_msg = await _quick_test_mcp_connection(cfg)
            if not success:
                raise Exception(error_msg)

            if cfg.get("transport") != "streamable_http":
                # SSE transport method
                self._streams_context = sse_client(
                    url=cfg["url"],
                    headers=cfg.get("headers", {}),
                    timeout=cfg.get("timeout", 5),
                    sse_read_timeout=cfg.get("sse_read_timeout", 60 * 5),
                )
                streams = await self.exit_stack.enter_async_context(
                    self._streams_context
                )

                # Create a new client session
                read_timeout = timedelta(seconds=cfg.get("session_read_timeout", 20))
                self.session = await self.exit_stack.enter_async_context(
                    mcp.ClientSession(
                        *streams,
                        read_timeout_seconds=read_timeout,
                        logging_callback=logging_callback,  # type: ignore
                    )
                )
            else:
                timeout = timedelta(seconds=cfg.get("timeout", 30))
                sse_read_timeout = timedelta(
                    seconds=cfg.get("sse_read_timeout", 60 * 5)
                )
                self._streams_context = streamablehttp_client(
                    url=cfg["url"],
                    headers=cfg.get("headers", {}),
                    timeout=timeout,
                    sse_read_timeout=sse_read_timeout,
                    terminate_on_close=cfg.get("terminate_on_close", True),
                )
                read_s, write_s, _ = await self.exit_stack.enter_async_context(
                    self._streams_context
                )

                # Create a new client session
                read_timeout = timedelta(seconds=cfg.get("session_read_timeout", 20))
                self.session = await self.exit_stack.enter_async_context(
                    mcp.ClientSession(
                        read_stream=read_s,
                        write_stream=write_s,
                        read_timeout_seconds=read_timeout,
                        logging_callback=logging_callback,  # type: ignore
                    )
                )

        else:
            server_params = mcp.StdioServerParameters(
                **cfg,
            )

            def callback(msg: str):
                # 处理 MCP 服务的错误日志
                self.server_errlogs.append(msg)

            stdio_transport = await self.exit_stack.enter_async_context(
                mcp.stdio_client(
                    server_params,
                    errlog=LogPipe(
                        level=logging.ERROR,
                        logger=logger,
                        identifier=f"MCPServer-{name}",
                        callback=callback,
                    ),  # type: ignore
                ),
            )

            # Create a new client session
            self.session = await self.exit_stack.enter_async_context(
                mcp.ClientSession(*stdio_transport)
            )
        await self.session.initialize()

    async def list_tools_and_save(self) -> mcp.ListToolsResult:
        """List all tools from the server and save them to self.tools"""
        response = await self.session.list_tools()
        self.tools = response.tools
        return response

    async def cleanup(self):
        """Clean up resources"""
        await self.exit_stack.aclose()
        self.running_event.set()  # Set the running event to indicate cleanup is done


class FuncCall:
    def __init__(self) -> None:
        self.func_list: List[FuncTool] = []
        """内部加载的 func tools"""
        self.mcp_client_dict: Dict[str, MCPClient] = {}
        """MCP 服务列表"""
        self.mcp_client_event: Dict[str, asyncio.Event] = {}

    def empty(self) -> bool:
        return len(self.func_list) == 0

    def add_func(
        self,
        name: str,
        func_args: list,
        desc: str,
        handler: Awaitable,
    ) -> None:
        """添加函数调用工具

        @param name: 函数名
        @param func_args: 函数参数列表，格式为 [{"type": "string", "name": "arg_name", "description": "arg_description"}, ...]
        @param desc: 函数描述
        @param func_obj: 处理函数
        """
        # check if the tool has been added before
        self.remove_func(name)

        params = {
            "type": "object",  # hard-coded here
            "properties": {},
        }
        for param in func_args:
            params["properties"][param["name"]] = {
                "type": param["type"],
                "description": param["description"],
            }
        _func = FuncTool(
            name=name,
            parameters=params,
            description=desc,
            handler=handler,
        )
        self.func_list.append(_func)
        logger.info(f"添加函数调用工具: {name}")

    def remove_func(self, name: str) -> None:
        """
        删除一个函数调用工具。
        """
        for i, f in enumerate(self.func_list):
            if f.name == name:
                self.func_list.pop(i)
                break

    def get_func(self, name) -> FuncTool:
        for f in self.func_list:
            if f.name == name:
                return f
        return None

    async def init_mcp_clients(self) -> None:
        """从项目根目录读取 mcp_server.json 文件，初始化 MCP 服务列表。文件格式如下：
        ```
        {
            "mcpServers": {
                "weather": {
                    "command": "uv",
                    "args": [
                        "--directory",
                        "/ABSOLUTE/PATH/TO/PARENT/FOLDER/weather",
                        "run",
                        "weather.py"
                    ]
                }
            }
            ...
        }
        ```
        """
        data_dir = get_astrbot_data_path()

        mcp_json_file = os.path.join(data_dir, "mcp_server.json")
        if not os.path.exists(mcp_json_file):
            # 配置文件不存在错误处理
            with open(mcp_json_file, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_MCP_CONFIG, f, ensure_ascii=False, indent=4)
            logger.info(f"未找到 MCP 服务配置文件，已创建默认配置文件 {mcp_json_file}")
            return

        mcp_server_json_obj: Dict[str, Dict] = json.load(
            open(mcp_json_file, "r", encoding="utf-8")
        )["mcpServers"]

        for name in mcp_server_json_obj.keys():
            cfg = mcp_server_json_obj[name]
            if cfg.get("active", True):
                event = asyncio.Event()
                asyncio.create_task(
                    self._init_mcp_client_task_wrapper(name, cfg, event)
                )
                self.mcp_client_event[name] = event

    async def _init_mcp_client_task_wrapper(
        self,
        name: str,
        cfg: dict,
        event: asyncio.Event,
        ready_future: asyncio.Future = None,
    ) -> None:
        """初始化 MCP 客户端的包装函数，用于捕获异常"""
        try:
            await self._init_mcp_client(name, cfg)
            tools = await self.mcp_client_dict[name].list_tools_and_save()
            if ready_future and not ready_future.done():
                # tell the caller we are ready
                ready_future.set_result(tools)
            await event.wait()
            logger.info(f"收到 MCP 客户端 {name} 终止信号")
        except Exception as e:
            logger.error(f"初始化 MCP 客户端 {name} 失败", exc_info=True)
            if ready_future and not ready_future.done():
                ready_future.set_exception(e)
        finally:
            # 无论如何都能清理
            await self._terminate_mcp_client(name)

    async def _init_mcp_client(self, name: str, config: dict) -> None:
        """初始化单个MCP客户端"""
        # 先清理之前的客户端，如果存在
        if name in self.mcp_client_dict:
            await self._terminate_mcp_client(name)

        mcp_client = MCPClient()
        mcp_client.name = name
        self.mcp_client_dict[name] = mcp_client
        await mcp_client.connect_to_server(config, name)
        tools_res = await mcp_client.list_tools_and_save()
        logger.debug(f"MCP server {name} list tools response: {tools_res}")
        tool_names = [tool.name for tool in tools_res.tools]

        # 移除该MCP服务之前的工具（如有）
        self.func_list = [
            f
            for f in self.func_list
            if not (f.origin == "mcp" and f.mcp_server_name == name)
        ]

        # 将 MCP 工具转换为 FuncTool 并添加到 func_list
        for tool in mcp_client.tools:
            func_tool = FuncTool(
                name=tool.name,
                parameters=tool.inputSchema,
                description=tool.description,
                origin="mcp",
                mcp_server_name=name,
                mcp_client=mcp_client,
            )
            self.func_list.append(func_tool)

        logger.info(f"已连接 MCP 服务 {name}, Tools: {tool_names}")

    async def _terminate_mcp_client(self, name: str) -> None:
        """关闭并清理MCP客户端"""
        if name in self.mcp_client_dict:
            try:
                # 关闭MCP连接
                await self.mcp_client_dict[name].cleanup()
                self.mcp_client_dict.pop(name)
            except Exception as e:
                logger.error(f"清空 MCP 客户端资源 {name}: {e}。")
            # 移除关联的FuncTool
            self.func_list = [
                f
                for f in self.func_list
                if not (f.origin == "mcp" and f.mcp_server_name == name)
            ]
            logger.info(f"已关闭 MCP 服务 {name}")

    @staticmethod
    async def test_mcp_server_connection(config: dict) -> list[str]:
        if "url" in config:
            success, error_msg = await _quick_test_mcp_connection(config)
            if not success:
                raise Exception(error_msg)

        mcp_client = MCPClient()
        try:
            logger.debug(f"testing MCP server connection with config: {config}")
            await mcp_client.connect_to_server(config, "test")
            tools_res = await mcp_client.list_tools_and_save()
            tool_names = [tool.name for tool in tools_res.tools]
        finally:
            logger.debug("Cleaning up MCP client after testing connection.")
            await mcp_client.cleanup()
        return tool_names

    async def enable_mcp_server(
        self,
        name: str,
        config: dict,
        event: asyncio.Event | None = None,
        ready_future: asyncio.Future | None = None,
        timeout: int = 30,
    ) -> None:
        """Enable_mcp_server a new MCP server to the manager and initialize it.

        Args:
            name (str): The name of the MCP server.
            config (dict): Configuration for the MCP server.
            event (asyncio.Event): Event to signal when the MCP client is ready.
            ready_future (asyncio.Future): Future to signal when the MCP client is ready.
            timeout (int): Timeout for the initialization.
        Raises:
            TimeoutError: If the initialization does not complete within the specified timeout.
            Exception: If there is an error during initialization.
        """
        if not event:
            event = asyncio.Event()
        if not ready_future:
            ready_future = asyncio.Future()
        if name in self.mcp_client_dict:
            return
        asyncio.create_task(
            self._init_mcp_client_task_wrapper(name, config, event, ready_future)
        )
        try:
            await asyncio.wait_for(ready_future, timeout=timeout)
        finally:
            self.mcp_client_event[name] = event

        if ready_future.done() and ready_future.exception():
            exc = ready_future.exception()
            if exc is not None:
                raise exc

    async def disable_mcp_server(
        self, name: str | None = None, timeout: float = 10
    ) -> None:
        """Disable an MCP server by its name.

        Args:
            name (str): The name of the MCP server to disable. If None, ALL MCP servers will be disabled.
            timeout (int): Timeout.
        """
        if name:
            if name not in self.mcp_client_event:
                return
            client = self.mcp_client_dict.get(name)
            self.mcp_client_event[name].set()
            if not client:
                return
            client_running_event = client.running_event
            try:
                await asyncio.wait_for(client_running_event.wait(), timeout=timeout)
            finally:
                self.mcp_client_event.pop(name, None)
                self.func_list = [
                    f
                    for f in self.func_list
                    if f.origin != "mcp" or f.mcp_server_name != name
                ]
        else:
            running_events = [
                client.running_event.wait() for client in self.mcp_client_dict.values()
            ]
            for key, event in self.mcp_client_event.items():
                event.set()
            # waiting for all clients to finish
            try:
                await asyncio.wait_for(asyncio.gather(*running_events), timeout=timeout)
            finally:
                self.mcp_client_event.clear()
                self.mcp_client_dict.clear()
                self.func_list = [f for f in self.func_list if f.origin != "mcp"]

    def get_func_desc_openai_style(self, omit_empty_parameter_field=False) -> list:
        """
        获得 OpenAI API 风格的**已经激活**的工具描述
        """
        _l = []
        # 处理所有工具（包括本地和MCP工具）
        for f in self.func_list:
            if not f.active:
                continue
            func_ = {
                "type": "function",
                "function": {
                    "name": f.name,
                    # "parameters": f.parameters,
                    "description": f.description,
                },
            }
            func_["function"]["parameters"] = f.parameters
            if not f.parameters.get("properties") and omit_empty_parameter_field:
                # 如果 properties 为空，并且 omit_empty_parameter_field 为 True，则删除 parameters 字段
                del func_["function"]["parameters"]
            _l.append(func_)
        return _l

    def get_func_desc_anthropic_style(self) -> list:
        """
        获得 Anthropic API 风格的**已经激活**的工具描述
        """
        tools = []
        for f in self.func_list:
            if not f.active:
                continue

            # Convert internal format to Anthropic style
            tool = {
                "name": f.name,
                "description": f.description,
                "input_schema": {
                    "type": "object",
                    "properties": f.parameters.get("properties", {}),
                    # Keep the required field from the original parameters if it exists
                    "required": f.parameters.get("required", []),
                },
            }
            tools.append(tool)
        return tools

    def get_func_desc_google_genai_style(self) -> dict:
        """
        获得 Google GenAI API 风格的**已经激活**的工具描述
        """

        # Gemini API 支持的数据类型和格式
        supported_types = {
            "string",
            "number",
            "integer",
            "boolean",
            "array",
            "object",
            "null",
        }
        supported_formats = {
            "string": {"enum", "date-time"},
            "integer": {"int32", "int64"},
            "number": {"float", "double"},
        }

        def convert_schema(schema: dict) -> dict:
            """转换 schema 为 Gemini API 格式"""

            # 如果 schema 包含 anyOf，则只返回 anyOf 字段
            if "anyOf" in schema:
                return {"anyOf": [convert_schema(s) for s in schema["anyOf"]]}

            result = {}

            if "type" in schema and schema["type"] in supported_types:
                result["type"] = schema["type"]
                if "format" in schema and schema["format"] in supported_formats.get(
                    result["type"], set()
                ):
                    result["format"] = schema["format"]
            else:
                # 暂时指定默认为null
                result["type"] = "null"

            support_fields = {
                "title",
                "description",
                "enum",
                "minimum",
                "maximum",
                "maxItems",
                "minItems",
                "nullable",
                "required",
            }
            result.update({k: schema[k] for k in support_fields if k in schema})

            if "properties" in schema:
                properties = {}
                for key, value in schema["properties"].items():
                    prop_value = convert_schema(value)
                    if "default" in prop_value:
                        del prop_value["default"]
                    properties[key] = prop_value

                if properties:  # 只在有非空属性时添加
                    result["properties"] = properties

            if "items" in schema:
                result["items"] = convert_schema(schema["items"])

            return result

        tools = [
            {
                "name": f.name,
                "description": f.description,
                **({"parameters": convert_schema(f.parameters)}),
            }
            for f in self.func_list
            if f.active
        ]

        declarations = {}
        if tools:
            declarations["function_declarations"] = tools
        return declarations

    async def func_call(self, question: str, session_id: str, provider) -> tuple:
        _l = []
        for f in self.func_list:
            if not f.active:
                continue
            _l.append(
                {
                    "name": f.name,
                    "parameters": f.parameters,
                    "description": f.description,
                }
            )
        func_definition = json.dumps(_l, ensure_ascii=False)

        prompt = textwrap.dedent(f"""
            ROLE:
            你是一个 Function calling AI Agent, 你的任务是将用户的提问转化为函数调用。

            TOOLS:
            可用的函数列表:

            {func_definition}

            LIMIT:
            1. 你返回的内容应当能够被 Python 的 json 模块解析的 Json 格式字符串。
            2. 你的 Json 返回的格式如下：`[{{"name": "<func_name>", "args": <arg_dict>}}, ...]`。参数根据上面提供的函数列表中的参数来填写。
            3. 允许必要时返回多个函数调用，但需保证这些函数调用的顺序正确。
            4. 如果用户的提问中不需要用到给定的函数，请直接返回 `{{"res": False}}`。

            EXAMPLE:
            1. `用户提问`：请问一下天气怎么样？ `函数调用`：[{{"name": "get_weather", "args": {{"city": "北京"}}}}]

            用户的提问是：{question}
        """)

        _c = 0
        while _c < 3:
            try:
                res = await provider.text_chat(prompt, session_id)
                if res.find("```") != -1:
                    res = res[res.find("```json") + 7 : res.rfind("```")]
                res = json.loads(res)
                break
            except Exception as e:
                _c += 1
                if _c == 3:
                    raise e
                if "The message you submitted was too long" in str(e):
                    raise e

        if "res" in res and not res["res"]:
            return "", False

        tool_call_result = []
        for tool in res:
            # 说明有函数调用
            func_name = tool["name"]
            args = tool["args"]
            # 调用函数
            func_tool = self.get_func(func_name)
            if not func_tool:
                raise Exception(f"Request function {func_name} not found.")

            ret = await func_tool.execute(**args)
            if ret:
                tool_call_result.append(str(ret))
        return tool_call_result, True

    def __str__(self):
        return str(self.func_list)

    def __repr__(self):
        return str(self.func_list)
