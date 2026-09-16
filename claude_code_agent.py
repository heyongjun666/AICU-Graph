import json
import subprocess
import os
import signal
import threading
from pathlib import Path
from typing import Callable, Optional, List

from cli_adapters import detect_default, get_adapter, native_base_url, provider_slot_base_url
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
                 api_key_env: Optional[str] = None):
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
            调用方需自行在 workspace/.claude/skills/ 准备好 skill 软链接/拷贝。
        """
        self.workspace = Path(workspace_path).resolve()
        self.model = model
        self.skip_permissions = skip_permissions
        self.enable_project_skills = enable_project_skills
        self.output_callback = output_callback
        self.api_base_url = api_base_url
        self.api_key_env = api_key_env
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
        self.adapter.provision(
            api_base_url=self.api_base_url,
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
