import subprocess
import os
import signal
import threading
from pathlib import Path
from typing import Callable, Optional, List

from cli_adapters import detect_default, get_adapter
from runtime.credentials import resolve_sk_for_model


class CCExecutionError(RuntimeError):
    """internal/claude 返回 error_during_execution 时的专用异常"""
    pass


def detect_cli() -> str:
    """自动检测可用的 CLI 工具，按优先级返回默认项（找不到则抛出异常）。"""
    return detect_default()


class ClaudeCodeAgent:
    """
    Claude Code 的编程式封装类，支持多轮对话 + 父子 Agent 编排
    兼容 internal 和 claude 两种 CLI
    """

    def __init__(self,
                 workspace_path: str,
                 instruction_md_path: Optional[str] = None,
                 model: str = "DeepSeek-V4-Flash",
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
        :param model: 使用的模型
        :param skip_permissions: 是否跳过权限确认（自动化时设为 True）
        :param cli_name: 指定 CLI 名称（"internal" 或 "claude"），为 None 时自动检测
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

        # 第三方端点（api_base_url）需预写 CLI 的 provider 配置（codex/qwen），
        # 使运行时 --model 能命中；internal/claude/kimi 走环境变量，provision 为 no-op。
        self.adapter.provision(
            api_base_url=self.api_base_url,
            api_key_env=self.api_key_env,
            model=self.model,
        )

        # 确保工作目录存在
        self.workspace.mkdir(parents=True, exist_ok=True)

        # 如果传入了 MD 文件，将其内容写入 workspace/CLAUDE.md
        # Claude Code / internal 会自动读取 CLAUDE.md 作为项目指令
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
            model=self.model,
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

    def _build_process_env(self) -> dict[str, str]:
        """Build child-process configuration without exposing credential values."""
        env = self.adapter.build_env(
            os.environ.copy(),
            api_base_url=self.api_base_url,
            api_key_env=self.api_key_env,
        )
        # 非 internal CLI：把 UI 保存的 sk（按模型厂商）注入到该 CLI 的认证环境变量，
        # 覆盖环境变量中的同名值。internal 走账号认证（--username），不注入 sk。
        if self.cli_name != "internal":
            sk = resolve_sk_for_model(self.model)
            if sk:
                env_name = self.adapter.api_key_env_for_model(self.model)
                if env_name:
                    env[env_name] = sk
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
        # 超时时用 os.killpg kill 整个进程组，避免 internal fork 的 claude-go 成为孤儿进程
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
            # 超时：kill 整个进程组（包括 internal 和它 fork 的 claude-go）
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
            # 重置对话上下文，避免续接到损坏的会话
            # （pause 场景通过 _preserve_conversation 阻止重置）
            if not self._preserve_conversation.is_set():
                self.conversation_id = None
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
            raise RuntimeError(f"{self.cli_name} 执行失败 ({detail})")

        result_stdout = stdout
        output = self.adapter.parse_result(result_stdout)
        if output is None:
            return result_stdout.strip()

        # 检测 error_during_execution：internal 内部出错但进程未崩溃
        subtype = output.get("subtype", "")
        if subtype == "error_during_execution":
            errors = output.get("errors") or []
            if isinstance(errors, str):
                errors = [errors]
            error_msg = "; ".join(errors) if errors else "未知内部错误"
            print(f"[CC] error_during_execution: {error_msg}")
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
        开启交互式会话（相当于直接敲 `internal` / `claude`）
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
