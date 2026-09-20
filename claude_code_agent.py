import json
import subprocess
import os
import signal
import threading
from pathlib import Path
from typing import Callable, Optional, List

from cli_adapters import (
    detect_default,
    get_adapter,
    mcp_enabled_clis,
    native_base_url,
    provider_slot_base_url,
)
from runtime.credentials import resolve_sk_for_model


class CCExecutionError(RuntimeError):
    """Claude Code CLI 返回 error_during_execution 时的专用异常"""
    pass


class ModelImageInputError(CCExecutionError):
    """模型不支持图片输入（读图触发 400 api_error）—— 模型能力限制而非会话损坏。

    与 CCExecutionError 的差异：触发时 conversation_id 应被尽力保留，
    供上层通过 --resume 续接同一会话并给出『跳过图片』指令后重试。
    """
    pass


# 模型不支持图片输入的 CLI 失败签名（如读截图做视觉验收触发 400）。
_MODEL_IMAGE_INPUT_ERROR_SIGNATURES = ("support image input",)


def is_model_image_input_error(text: str) -> bool:
    """判断 CLI 失败信息是否为『模型不支持图片输入』（读图喂给模型时报 400）。"""
    return any(sig in text for sig in _MODEL_IMAGE_INPUT_ERROR_SIGNATURES)


def detect_cli() -> str:
    """自动检测可用的 CLI 工具，按优先级返回默认项（找不到则抛出异常）。"""
    return detect_default()


def _drop_transient(paths: list, stop=None) -> None:
    """删掉随 CLI 进程起落的临时配置文件（见 ``CliAdapter.mcp_workspace_files``）。

    为什么非删不可：这类文件只对「本次 CLI 进程」有意义，留在 workspace 里会被
    AICU 的产物收集当成交付物 —— ``.qwen`` 不在 ``NONCODE_DIRS`` 里，``.json`` 也不在
    ``NONCODE_EXTENSIONS`` 里，于是会被当作 CC 写的代码文件复制进最优方案目录、
    并出现在 ``_INDEX.md`` 的文件清单里。

    顺手删空目录：``.qwen/`` 这种空壳虽然收文件时看不见（``is_file()`` 会挡掉），
    但会在产物目录里留下没有解释的残迹。``rmdir`` 对非空目录抛 ``OSError``，正好
    用来表达「里面还有别人的东西，别动」—— 所以两种失败都安静吞掉。

    :param stop: 向上删空目录的**边界（不含）**，通常传 workspace 根。配置文件不止
        一层深时（比如某个 CLI 要 ``<dir>/<sub>/config.json``）只删最内层会剩下
        ``<dir>/<sub>/`` 和 ``<dir>/`` 两个空壳，而空壳正是这个函数要消灭的东西。
        **workspace 本身永远不会被删** —— 循环在 ``parent == stop`` 时停下。
        ``None`` = 调用方没给边界，只删一层（保持老行为）：没有边界还往上爬，
        等于让一次配置文件的清理波及整条目录链。

        当前接线的 CLI 里没有一个用到多层路径（agy 的 ``.agents/plugins/aicu/`` 是
        当初写这版的起因，但那一版最终没接 —— 见 AntigravityAdapter），所以这一路
        对现有 CLI 是行为等价的；留着是因为「配置文件的深度」是 adapter 的自由，
        而少删一层是个只在产物目录里才看得见的缺陷。

        **不在 ``stop`` 之下的路径只删文件、不删目录**：这类文件是 adapter 写在
        workspace 之外的（kimi 的 workspace 信任记录落在 ``~/.kimi-code/workspace-trust/``），
        文件是我写的，目录不是 —— 那个目录属于 kimi，里面还有用户自己的记录。
        ``rmdir`` 一旦碰上恰好空的目录就会把别人的目录端掉，所以这一路直接跳过。
    """
    for path in paths or []:
        try:
            path = Path(path)
            path.unlink(missing_ok=True)
            if stop is None:
                path.parent.rmdir()
                continue
            boundary = Path(stop)
            # stop 不在本文件的目录链上时只删文件、不动目录：这种文件是我们写的，它
            # 所在的**目录不是**（kimi 的 workspace 信任记录就落在 ~/.kimi-code/
            # workspace-trust/ 里，那是 kimi 自己的目录，还装着用户其它记录）。
            # 沿一条不属于自己的目录链 rmdir 是纯粹的误伤，宁可留着空目录。
            if boundary not in path.parents:
                continue
            parent = path.parent
            while parent != boundary:
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass


class ClaudeCodeAgent:
    """
    Claude Code 的编程式封装类，支持多轮对话 + 父子 Agent 编排
    """

    def __init__(self,
                 workspace_path: str,
                 instruction_md_path: Optional[str] = None,
                 model: Optional[str] = None,
                 skip_permissions: bool = False,
                 cli_name: Optional[str] = None,
                 enable_project_skills: bool = False,
                 output_callback: Optional[Callable[[str, str], None]] = None,
                 api_base_url: Optional[str] = None,
                 api_key_env: Optional[str] = None,
                 mcp_servers: Optional[list] = None):
        """
        初始化一个 CC 实例

        :param workspace_path: 工作目录（必填），会作为 --cwd 参数
        :param instruction_md_path: 角色说明 MD 文件路径（可选），内容会写入 workspace/CLAUDE.md
        :param model: 使用的模型（None 时不传 --model，由目标 CLI 使用自身默认；
                     无代码兜底默认模型）
        :param skip_permissions: 是否跳过权限确认（自动化时设为 True）
        :param cli_name: 指定 CLI 名称，为 None 时自动检测
        :param enable_project_skills: 是否启用 project-level skill 加载。
            True 时命令追加 --setting-sources project，使 CC 只加载 workspace
            下 .claude/skills/ 中的 skill，不加载用户级 ~/.claude/skills/。
            **只有 claude 系 CLI 走这条 argv 通道**；其余 CLI 各自读自己名字下的
            项目级 skill 目录（.qwen/skills、.pi/skills、…），由 AICU 侧按
            ``CliAdapter.skills_workspace_dir`` 落盘，与这个参数无关。
        :param mcp_servers: 要下发给 CLI 的 MCP server 列表（``list[McpServer]``），
            由 AICU 侧完成注册表解析（runtime/mcp_manager.py）与连通性预检
            （runtime/mcp_client.py）。
            **这里刻意收对象而不是 JSON 串**：三家 CLI 的配置形态互不相同（CC 族是
            argv 里的 mcpServers JSON、codex 是一串 -c 键值、opencode 是环境变量里的
            另一份 JSON），序列化只有 adapter 知道该怎么做。早先传 JSON 串还得反解出
            名字给权限白名单用，多接一家就撑不住了。
        """
        self.workspace = Path(workspace_path).resolve()
        self.model = model
        self.skip_permissions = skip_permissions
        self.enable_project_skills = enable_project_skills
        self.output_callback = output_callback
        self.api_base_url = api_base_url
        self.api_key_env = api_key_env
        # 权限白名单（CC 族）必须与实际下发的 server 逐字对应，所以名字就在 adapter 里
        # 从同一个列表上取，而不是在这里另算一份传下去 —— 同源才不可能对不上。
        self.mcp_servers = list(mcp_servers or [])
        self.conversation_id: Optional[str] = None
        self._last_process_pid: Optional[int] = None
        # pause/resume 时设为 True，阻止 chat() 重置 conversation_id
        self._preserve_conversation = threading.Event()

        # 确定 CLI adapter：显式指定 > 自动检测
        if cli_name:
            try:
                self.adapter = get_adapter(cli_name)
            except ValueError as error:
                raise RuntimeError(str(error)) from error
            if not self.adapter.is_installed():
                raise RuntimeError(f"指定的 CLI '{cli_name}' 未找到，请确认已安装")
        else:
            self.adapter = get_adapter(detect_cli())
        self.cli_name = self.adapter.id

        # 角色声明了 MCP 但落到一个 AICU 还没接线的 CLI 上时，直接报错而不是静默忽略。
        # 静默忽略的后果是最坏的：任务跑完了、看起来正常，但工具从来不存在 ——
        # 用户以为配好了。skill 曾经就是这个问题（软链照建、永不生效），区别在于
        # skills 那侧刻意**只告警不报错**（少份参考资料 vs 工具根本不存在，见 aicu.py
        # 的 _warn_skills_unwired）。
        #
        # 文案刻意说「尚未接入」而不是「不支持」：codex / qwen / agy 这些自己都有 MCP，
        # 只是注入方式各不相同、AICU 还没接线。说成「该 CLI 不支持」是关于 CLI 的假话，
        # 用户会跑去换 CLI，而不是来问我们。
        #
        # 具体原因由 adapter 给（``mcp_unwired_reason``），因为「为什么没接」只有 adapter
        # 知道，而且未必是「AICU 还没接」：pi 就是现成的反例 —— 注入路径我们这边是齐的，
        # 缺的是用户本机那个扩展，所以它的原因短语给的是**装扩展的命令**。把用户指向一个
        # 他做得了的动作，是留这个钩子的全部理由。（grok / antigravity 的成因写在各自的
        # docstring 里，走基类默认句。）
        if self.mcp_servers and not self.adapter.supports_mcp:
            raise RuntimeError(
                f"CLI '{self.cli_name}' 的 MCP {self.adapter.mcp_unwired_reason()}，"
                f"但本次运行声明了 MCP server。"
                # 「也可以」而非「请」：原因是「装个扩展就行」时，上面那句已经给了正确的
                # 动作，这里再写「请改用」就成了与它抢注意力的第二道指令。降级成备选之后，
                # 两条成因（要用户自己动手 / AICU 确实还没接）读起来都顺。
                f"本次运行也可以改用已接入 MCP 的 CLI（{', '.join(mcp_enabled_clis())}），"
                f"或从角色配置中移除 mcp 字段。"
            )

        # 模型厂商与 CLI 原生协议不一致时（例如 claude CLI 选了 GLM），把 base_url
        # 指向该厂商对应协议的原生端点。厂商自己就实现这套协议，不需要任何翻译。
        # 放在 provision 之前是关键：provision 与 _build_process_env 都读
        # self.api_base_url，改这一处即可同时覆盖两条下游路径。
        configured_base_url = self.api_base_url
        native_endpoint = native_base_url(self.cli_name, self.model, configured_base_url)
        if native_endpoint:
            self.api_base_url = native_endpoint

        # 模型名交给 CLI 时的写法可能与 AICU 的模型名不同（opencode / pi 要求
        # <provider>/<model>，前缀正是 AICU 注册第三方端点用的 provider id），
        # 由 adapter 统一换算。必须在 native_base_url 之后算 —— 换不换算取决于
        # 本次是否真的改道了。
        self.cli_model_arg = self.adapter.model_arg(self.model, api_base_url=self.api_base_url)

        # 端点落在 CLI 配置文件里的托管槽（codex / qwen / pi）与「CLI 自身寻址」是两回事：
        # 那个槽只有 AICU 会写，留着上次的值就是留着一份过期的事实，所以它的取值单独算，
        # 每次都给出确定答案 —— 要么按当前模型所属厂商重写，要么撤掉自己写过的
        # （返回 None 即撤槽，见 provider_slot_base_url）。
        # 走环境变量 / 内联配置的 CLI（claude / kimi / opencode / codebuddy）没有槽，
        # 这里恒为 None，provision 是 no-op，它们的行为与改动前一致。
        #
        # 喂进去的是**改写后**的 self.api_base_url，不是 configured_base_url：预置地址
        # 不算「用户自己指定的端点」，上面那步已经按模型厂商把它改道了，槽必须跟着同一个
        # 值走。照抄配置值会让两条通道各说各话 —— codex 读的正是槽（顶层
        # model_provider = "aicu" 指过去的那块），于是「选了 OpenAI 预置再把模型换成
        # deepseek」会把 deepseek 的模型名发到 api.openai.com 去。
        # 用户手填的地址不受影响：native_base_url 对非预置地址返回 None，self.api_base_url
        # 仍是原值。同族时 self.api_base_url 为 None 也不影响槽 —— provider_slot_base_url
        # 自己会把同族该接管的那份端点算出来（qwen 同族照样接管，codex 同族撤槽）。
        self.adapter.provision(
            api_base_url=provider_slot_base_url(
                self.cli_name, self.model, self.api_base_url
            ),
            api_key_env=self.api_key_env,
            model=self.model,
        )

        # 确保工作目录存在
        self.workspace.mkdir(parents=True, exist_ok=True)

        # 如果传入了 MD 文件，将其内容写入 workspace/CLAUDE.md
        # Claude Code 会自动读取 CLAUDE.md 作为项目指令
        if instruction_md_path:
            md_file = Path(instruction_md_path).resolve()
            if not md_file.exists():
                raise FileNotFoundError(f"说明文件不存在: {instruction_md_path}")

            with open(md_file, 'r', encoding='utf-8') as f:
                instruction_content = f.read().strip()

            claude_md_path = self.workspace / "CLAUDE.md"
            if claude_md_path.exists():
                existing = claude_md_path.read_text(encoding='utf-8').strip()
                if existing != instruction_content:
                    print(f"[CC] 警告: {claude_md_path} 已存在且内容不同，将被覆盖")

            with open(claude_md_path, 'w', encoding='utf-8') as f:
                f.write(instruction_content)
            print(f"[CC] 已将角色说明写入: {claude_md_path}")

        print(f"[CC] 初始化成功，CLI: {self.cli_name}，工作目录: {self.workspace}")

    def _build_base_command(self) -> List[str]:
        """构建基础的 CLI 命令参数（不包含 --cwd，工作目录由 subprocess.run(cwd=) 控制）"""
        return self.adapter.build_base_command(
            model=self.cli_model_arg,
            skip_permissions=self.skip_permissions,
            enable_project_skills=self.enable_project_skills,
            mcp_servers=self.mcp_servers,
        )

    def _communicate_with_stream(
        self, proc: subprocess.Popen, prompt: Optional[str], timeout: int
    ) -> tuple[str, str]:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def drain(stream, label: str, parts: list[str]) -> None:
            for line in iter(stream.readline, ""):
                parts.append(line)
                if not self.output_callback:
                    continue
                message = line.rstrip("\r\n")
                # Internal 2.1.x 当前会在 SDK 查询成功时把此兼容性告警写入
                # stderr；它不是执行失败，不应作为任务内容转发给 AICU。
                if (
                    label == "stderr"
                    and self.cli_name == "internal"
                    and message.startswith("[claude-code:unrecognized_model]")
                ):
                    continue
                if label == "stdout":
                    message = self.adapter.format_stream_event(message)
                if message:
                    self.output_callback(label, message)

        if prompt is not None:
            proc.stdin.write(prompt)
        proc.stdin.close()
        stdout_thread = threading.Thread(
            target=drain, args=(proc.stdout, "stdout", stdout_parts), daemon=True
        )
        stderr_thread = threading.Thread(
            target=drain, args=(proc.stderr, "stderr", stderr_parts), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        proc.wait(timeout=timeout)
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        return "".join(stdout_parts), "".join(stderr_parts)

    @staticmethod
    def _format_stream_event(line: str) -> str:
        """Claude Code 流事件格式化（保留 staticmethod 兼容外部调用与既有测试）。"""
        return get_adapter("claude").format_stream_event(line)

    @staticmethod
    def _parse_result_output(raw: str) -> Optional[dict]:
        """Claude Code 结果解析（归一化；保留 staticmethod 兼容外部调用）。"""
        return get_adapter("claude").parse_result(raw)

    @staticmethod
    def _extract_session_id_from_output(stdout: str) -> Optional[str]:
        """从 CLI 输出中尽力提取会话 ID。

        失败运行（如读图 400 api_error）的 result 事件通常不含 session_id，
        但 stream-json 的 system/init、user 等事件行可能携带；逐行扫描兜底，
        取第一个非空会话 ID。
        """
        if not stdout:
            return None
        normalized = ClaudeCodeAgent._parse_result_output(stdout)
        if normalized and normalized.get("session_id"):
            return normalized["session_id"]
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            for key in ("session_id", "sessionId", "conversation_id"):
                sid = value.get(key)
                if isinstance(sid, str) and sid:
                    return sid
        return None

    def _preserve_image_error_conversation(self, candidate: Optional[str]) -> None:
        """图片输入错误时尽力保留会话 ID，供上层 --resume 续接同一会话。

        续接调用（pause/resume，_preserve_conversation 已置位）时保留既有
        conversation_id（本次失败的就是该会话）；首次调用则用 candidate
        （从失败 stdout 提取的会话 ID）兜底。candidate 为空时保持现状，
        由上层决定降级为带『禁读图』指令的全新会话。
        """
        if not self._preserve_conversation.is_set():
            self.conversation_id = candidate or self.conversation_id

    def _build_process_env(self) -> dict[str, str]:
        """Build child-process configuration without exposing credential values."""
        # 注入 sk 用的变量名按模型厂商解析（多厂商 CLI 一个模型一个变量名）。
        # 这个名字同时交给 build_env：opencode 的 OPENCODE_CONFIG_CONTENT 与 pi 的
        # models.json 都把它写进自己的 provider 配置作引用，两边必须逐字一致，
        # 否则 CLI 拿不到 key（codex 的 env_key 踩过同一个坑）。
        sk = None
        key_env_name = None
        if self.cli_name != "internal":  # Internal 使用 --username 认证
            sk = resolve_sk_for_model(self.model)
            key_env_name = self.adapter.api_key_env_for_model(self.model)

        env = self.adapter.build_env(
            os.environ.copy(),
            api_base_url=self.api_base_url,
            api_key_env=key_env_name or self.api_key_env,
            model=self.model,
            mcp_servers=self.mcp_servers,
        )
        if sk and key_env_name:
            env[key_env_name] = sk
        return env

    def chat(self, prompt: str, max_turns: Optional[int] = None, timeout: int = 36000) -> str:
        """
        多轮对话：首次调用创建会话，后续调用自动续接上下文

        :param prompt: 本轮对话内容
        :param max_turns: Agent 最大执行轮数（含工具调用）
        :param timeout: 超时时间（秒），默认 36000（10 小时）
        :return: CC 的回复文本
        """
        cmd = self._build_base_command()
        streaming = bool(self.output_callback)
        cmd.extend(self.adapter.print_flags(streaming=streaming))
        cmd.extend(self.adapter.mcp_flags(self.mcp_servers))

        # 如果已有 conversation_id，通过 CLI 的 resume 参数续接上下文
        if self.conversation_id:
            cmd.extend(self.adapter.resume_flag(self.conversation_id))

        if max_turns:
            cmd.extend(self.adapter.max_turns_flag(max_turns))

        # Claude Code / kimi / codex 走 stdin 传 prompt（避免深层递归上下文快照超过
        # ARG_MAX，codex exec 也实测要求 stdin）；其它 exec 风格 CLI（qwen -p 等）
        # 用命令行参数传 prompt
        prompt_cmd, prompt_via_stdin = self.adapter.prompt_args(prompt)
        if prompt_cmd:
            cmd.extend(prompt_cmd)
        prompt_mode = "stdin" if prompt_via_stdin else "argv"
        print(f"[CC] 执行命令: {' '.join(cmd)} (prompt via {prompt_mode}, {len(prompt)} chars)")

        # 用 Popen + start_new_session=True 创建独立进程组，
        # 超时时用 os.killpg kill 整个进程组，避免 CLI 子进程成为孤儿进程
        #
        # 少数 CLI（qwen / gemini / cursor / pi / kimi / grok）只能从「cwd 下的项目级
        # 配置文件」读 MCP，没法走 argv/env。这类文件必须在 Popen 之前写好（CLI 启动
        # 时就要读），进程结束后立刻删掉，否则会被产物收集当成交付物（见 _drop_transient）。
        # 返回值也可能包含 workspace **之外**的文件 —— kimi 除了项目级配置还要补一条
        # workspace 信任记录（在 ~/.kimi-code/workspace-trust/ 下），少了它 kimi 会把
        # 项目级配置静默丢掉；这类路径在 _drop_transient 里只删文件不删目录。
        # stop=self.workspace：清空目录时不许爬出本次 CC 的 workspace。
        transient = self.adapter.mcp_workspace_files(self.workspace, self.mcp_servers)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.workspace,
                env=self._build_process_env(),
                start_new_session=True,  # 子进程成为新会话/进程组的组长
            )
        except BaseException:
            _drop_transient(transient, stop=self.workspace)
            raise
        self._last_process_pid = proc.pid
        try:
            prompt_input = prompt if prompt_via_stdin else None
            if streaming:
                stdout, stderr = self._communicate_with_stream(proc, prompt_input, timeout)
            else:
                stdout, stderr = proc.communicate(input=prompt_input, timeout=timeout)
        except subprocess.TimeoutExpired:
            # 超时：kill 整个进程组，避免子进程残留
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # 进程已退出
            proc.wait()
            print(f"[CC] 超时（>{timeout}s），已 kill 整个进程组")
            # 重置对话上下文，避免续接已损坏的会话
            # （pause 场景通过 _preserve_conversation 阻止重置）
            if not self._preserve_conversation.is_set():
                self.conversation_id = None
            self._last_process_pid = None
            raise
        finally:
            self._last_process_pid = None
            _drop_transient(transient, stop=self.workspace)

        if proc.returncode != 0:
            stdout_tail = (stdout or "").strip()
            stderr_tail = (stderr or "").strip()
            detail_parts = [f"returncode={proc.returncode}"]
            if stdout_tail:
                stdout_preview = stdout_tail[-500:] if len(stdout_tail) > 500 else stdout_tail
                detail_parts.append(f"stdout_tail={stdout_preview}")
            if stderr_tail:
                stderr_preview = stderr_tail[-500:] if len(stderr_tail) > 500 else stderr_tail
                detail_parts.append(f"stderr={stderr_preview}")
            detail = "; ".join(detail_parts)
            print(f"[CC] 执行出错: {detail}")
            # 模型不支持图片输入（读图触发 400）：模型能力限制而非会话损坏。
            # 尽力保留会话 ID 供上层 --resume 续接同一对话；提取不到则重置，
            # 由上层退化为带『禁读图』指令的全新会话重试。
            if is_model_image_input_error(detail):
                # 图片输入限制：保留会话 ID 供上层 --resume 续接（见 helper 语义）
                self._preserve_image_error_conversation(
                    self._extract_session_id_from_output(stdout or "")
                )
                raise ModelImageInputError(f"{self.cli_name} 执行失败 ({detail})")
            # 重置对话上下文，避免续接到损坏的会话
            # （pause 场景通过 _preserve_conversation 阻止重置）
            if not self._preserve_conversation.is_set():
                self.conversation_id = None
            raise RuntimeError(f"{self.cli_name} 执行失败 ({detail})")

        result_stdout = stdout
        output = self.adapter.parse_result(result_stdout)
        if output is None:
            return result_stdout.strip()

        # 检测 error_during_execution：CLI 内部出错但进程未崩溃
        subtype = output.get("subtype", "")
        if subtype == "error_during_execution":
            errors = output.get("errors") or []
            if isinstance(errors, str):
                errors = [errors]
            error_msg = "; ".join(errors) if errors else "未知内部错误"
            print(f"[CC] error_during_execution: {error_msg}")
            # 防御性分支：若图片输入限制以 error_during_execution 形式出现，
            # 同样保留会话 ID 供上层续接（实测该场景走 returncode!=0 路径）。
            if is_model_image_input_error(error_msg):
                # 防御性分支：图片输入限制以 error_during_execution 形式出现时
                # 同样保留会话 ID 供上层续接（实测该场景走 returncode!=0 路径）。
                self._preserve_image_error_conversation(output.get("session_id"))
                raise ModelImageInputError(f"{self.cli_name} 内部执行错误: {error_msg}")
            # 重置对话上下文，避免续接已损坏的会话
            self.conversation_id = None
            raise CCExecutionError(f"{self.cli_name} 内部执行错误: {error_msg}")

        self.conversation_id = output.get("session_id")
        return output.get("result") or result_stdout.strip()

    def reset_conversation(self):
        """重置对话上下文，下次 chat() 将开启全新会话"""
        self.conversation_id = None
        print("[CC] 对话上下文已重置")

    def spawn_child(self,
                    sub_workspace_name: str,
                    child_instruction_md: Optional[str] = None,
                    **kwargs) -> 'ClaudeCodeAgent':
        """
        在当前 workspace 下创建子 CC 实例

        :param sub_workspace_name: 子任务文件夹名称（在当前 workspace 内创建）
        :param child_instruction_md: 子 CC 专用的 MD 说明文件
        :param kwargs: 其他初始化参数（如 model、skip_permissions、cli_name）
        :return: 新的 ClaudeCodeAgent 对象
        """
        # 子目录路径 = 当前 workspace / 子文件夹名（在父目录内部，方便管控）
        child_workspace = self.workspace / sub_workspace_name

        child_agent = ClaudeCodeAgent(
            workspace_path=str(child_workspace),
            instruction_md_path=child_instruction_md,
            model=kwargs.get('model', self.model),
            skip_permissions=kwargs.get('skip_permissions', self.skip_permissions),
            cli_name=kwargs.get('cli_name', self.cli_name),
        )
        return child_agent

    def interactive(self):
        """
        开启交互式会话（相当于直接运行 CLI）
        注意：这会阻塞当前进程，直到用户退出
        """
        cmd = self._build_base_command()
        print(f"[CC] 启动交互会话: {' '.join(cmd)}")
        subprocess.run(cmd, cwd=self.workspace, env=self._build_process_env())

    # ---- 辅助方法 ----

    def execute_bash(self, bash_command: str) -> str:
        """在 workspace 内执行一条 Bash 命令"""
        result = subprocess.run(
            bash_command,
            shell=True,
            cwd=self.workspace,
            capture_output=True,
            text=True
        )
        return result.stdout.strip()

    def read_file(self, file_path: str) -> str:
        """读取工作目录下的文件内容"""
        target = self.workspace / file_path
        if target.exists():
            with open(target, 'r', encoding='utf-8') as f:
                return f.read()
        return ""
