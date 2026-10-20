"""MCP 配置的落盘格式：claude / codex / qwen / grok / opencode 各自吃的那一份。

从 ``cli_adapters`` 拆出的序列化层：只做「一串 McpServer -> 某个 CLI 认的文本」，
以及几处探测 / 信任记录的落盘。这里不建命令、不跑 agent 子进程。

注意远程条目的键名各家不统一（``url`` vs ``httpUrl``），而写错不会报错，只会静默
走成另一条传输 —— 具体差异见 ``_remote_fields`` 的 docstring。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from runtime.mcp_manager import TRANSPORT_HTTP, McpConfigError

def _remote_fields(server, url_key: str) -> dict:
    """远程（Streamable HTTP）条目里那不变的两样：地址与请求头。

    键名由调用方给，因为**各家对「远程地址」的键名并不统一**，这是接远程 MCP 最容易
    写错的一处：claude/codebuddy/codex/kimi/pi/cursor/opencode 用 ``url``，
    gemini/qwen 用 ``httpUrl``。后者尤其要小心 —— 它们没有 type 字段、传输由键名决定，
    ``url`` 在那边表示的是 **SSE**（被 Streamable HTTP 取代的旧传输），写错会静默
    走成另一种传输。

    ``headers`` 为空时整个键都不写，与 stdio 那边 ``env`` 的处理一致。
    """
    entry = {url_key: server.url}
    if server.headers:
        entry["headers"] = dict(server.headers)
    return entry


def _cc_mcp_json(servers: list) -> str:
    """Claude Code 协议族的 ``mcpServers`` 配置串。

    本地形状是 ``{"mcpServers": {<name>: {command, args, env?}}}`` —— ``command`` 是
    字符串、参数单独放 ``args``。注意这**不是**通用形状：opencode 要的是
    ``command: [程序, ...参数]`` 的数组形式，所以那边另写一份（见
    ``_opencode_config_content``）。共用一个序列化器只会让两边都错。

    远程形状是 ``{"type": "http", "url": ..., "headers": {...}}``。``type`` **必须写**：
    claude 把「有 url 却没有 type」的条目当成配置错误，会跳过整个 server 并报一句
    「has a "url" but no "type"」。本地条目则不能补 ``type: "stdio"`` —— 那是改动既有
    配置的字节。

    codebuddy 走同一条路径（同一个类的子类），也照这个形状发。
    """
    entries: dict = {}
    for server in servers:
        if server.transport == TRANSPORT_HTTP:
            entries[server.name] = {"type": "http", **_remote_fields(server, "url")}
            continue
        entry: dict = {"command": server.command, "args": list(server.args)}
        if server.env:
            entry["env"] = dict(server.env)
        entries[server.name] = entry
    return json.dumps({"mcpServers": entries}, ensure_ascii=False)


def _mcp_servers_project_json(servers: list, url_key: str = "url") -> str:
    """项目级 ``mcpServers`` 配置文件的内容（gemini / cursor / pi / kimi 同形）。

    本地形状与 ``_cc_mcp_json`` 一样是 ``command`` 字符串 + ``args`` 数组 + 可选 ``env``，
    差别只在渲染：这里是**落盘给 CLI 解析的文件**，所以缩进 2 空格并带尾换行（与
    ``_mcp_servers_settings_json`` 的落盘风格一致）。

    远程形状是 ``{"<url_key>": ..., "headers": {...}}``，**没有 type 字段**：这四家都
    按「给了哪个连接字段」推传输。``url_key`` 因此是参数而不是写死的 —— gemini 用
    ``httpUrl``（``url`` 在它那里表示 SSE，见 ``GeminiAdapter``），其余三家用 ``url``。
    两家的差异收敛成一个类属性，与 ``mcp_workspace_config`` 同一思路。

    为什么另起一份而不是复用/参数化上面那两个：

      - 不复用 ``_cc_mcp_json``：它绑在 claude 系的 argv 路径上（``--mcp-config=<紧凑
        JSON>``），改它等于动那四家已经接好的 CLI，而这一批的前提是「现有行为一个
        字节都不变」。
      - 不参数化 qwen 的 ``trust``：``trust: true`` 是 qwen 自己的无头批准语义
        （见 ``_mcp_servers_settings_json`` 的说明）。gemini 也有 ``trust`` 字段，但
        含义完全不同（"trusted servers may gain additional capabilities"，不是批准
        门禁）；cursor / pi / kimi 根本没有这个键。合并只会让某一方多写一个自己
        不认识或不期望的字段。

    四家同形是**核实过的事实**（各自的官方文档或发行包），不是「看着像」——
    所以这里共用一份是诚实的；哪天某一家分叉，就照 ``_opencode_config_content`` 的
    先例给它单独写一份。grok 走的是同一个钩子但内容是 TOML，另行渲染。
    """
    entries: dict = {}
    for server in servers:
        if server.transport == TRANSPORT_HTTP:
            entries[server.name] = _remote_fields(server, url_key)
            continue
        entry: dict = {"command": server.command, "args": list(server.args)}
        if server.env:
            entry["env"] = dict(server.env)
        entries[server.name] = entry
    return json.dumps({"mcpServers": entries}, ensure_ascii=False, indent=2) + "\n"


# ---- grok 的 TOML 发射 ---------------------------------------------------------
#
# grok 读的是 TOML（``.grok/config.toml``），仓库里没有 TOML 库，codex 那份也是手写
# 正则（见 :func:`_codex_mcp_args`），这里同样手写 —— 要写的形状是固定的，不需要
# 通用序列化器。
#
# **不能拿 json.dumps 冒充 TOML**：两者的转义规则不重合，而差集正好是会被静默吞掉的
# 那一类 ——
#   - U+007F(DEL)：JSON 允许直接写在字符串里（json.dumps 默认就不转义它），TOML 里
#     它是非法字面字符，只能写成 ``\u007F``；
#   - 代理对：JSON 那种 ``😀`` 写法在 TOML 里非法，所以非 ASCII 一律原样
#     输出，一个 ``\u`` 都不发。
# 用 fullmatch 而不是 ``^...$``：Python 的 ``$`` 也匹配「结尾换行之前」，于是名字里
# 带尾随 ``\n`` 时（``mcp_servers.json`` 里写 ``"echo-probe\n"``）会被判成裸键、把换行
# 原样吐进表名，整份 TOML 解析失败 —— 而且是**同文件里其它 server 一起丢**，
# grok 那边只表现为「工具不存在」。``\Z``/fullmatch 没有这个缺口。
_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")
_TOML_ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f",
}


def _toml_string(value: str) -> str:
    """渲染一个 TOML 基本字符串（含两侧引号）。"""
    out = []
    for ch in value:
        escaped = _TOML_ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ch < "\x20" or ch == "\x7f":
            # 其余控制符 + DEL：TOML 只认 \uXXXX 形式
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    """表名/键名：裸键只在 ``[A-Za-z0-9_-]+`` 时安全，其余一律加引号。

    加引号不是洁癖：server 名里的 ``.`` 会让 ``[mcp_servers.my.server]`` 被解析成
    三层嵌套表（mcp_servers → my → server），而不是一个叫 ``"my.server"`` 的 server。
    grok 那边于是找不到它，而且不会有任何报错 —— 正是要避免的那种静默失败。
    """
    return key if _TOML_BARE_KEY.match(key) else _toml_string(key)


def _grok_mcp_toml(servers: list) -> str:
    """grok ``.grok/config.toml`` 里的 ``[mcp_servers.<name>]`` 段。

    形状取自 grok 自带的用户手册（``~/.grok/docs/user-guide/07-mcp-servers.md``，
    由二进制解出）：

        [mcp_servers.github]
        command = "npx"
        args = ["-y", "@modelcontextprotocol/server-github"]
        env = { GITHUB_PERSONAL_ACCESS_TOKEN = "ghp_xxx" }

    ``env`` 用**行内表**，与手册一致。这不只是抄格式：写成子表
    ``[mcp_servers.x.env]`` 之后父表就不能再补键，顺序写错即非法。
    env 的值直接写解析好的字面量，**不套 grok 自己的 ``${VAR}`` 插值** ——
    那会让 grok 再查一次环境变量，而 AICU 给的已经是解析好的值。

    远程条目在这里**直接抛错**。这不是可达路径：grok 的 ``supports_mcp`` 是 False，
    基类的门禁根本走不到这个函数（见 ``mcp_workspace_files`` 的第一个 if）。留着它
    是为了让以后有人把 ``supports_mcp`` 翻成 True 时**当场炸** —— 那时若不拦，下面几行
    会照常执行，写出一份 ``command = ""``、``args = []`` 的配置：grok 读得进去、不报错、
    server 永远连不上，而用户手里唯一的线索是一个空命令。远程的 TOML 形状（grok 是否
    用 ``url``、要不要 ``transport`` 键）没有核实过，所以这里不猜着写，只拦住。
    """
    lines: list = []
    for server in servers:
        if server.transport == TRANSPORT_HTTP:
            raise McpConfigError(
                f"grok 的远程（Streamable HTTP）MCP 尚未接入 AICU：server '{server.name}' "
                f"只有 url 没有 command，写进 config.toml 会变成一条空命令。"
                f"请改用已支持远程的 CLI，或把这个 server 配成本地型"
            )
        lines.append(f"[mcp_servers.{_toml_key(server.name)}]")
        lines.append(f"command = {_toml_string(server.command)}")
        lines.append("args = [" + ", ".join(_toml_string(a) for a in server.args) + "]")
        if server.env:
            pairs = ", ".join(
                f"{_toml_key(k)} = {_toml_string(v)}" for k, v in server.env.items()
            )
            lines.append(f"env = {{ {pairs} }}")
        lines.append("")
    return "\n".join(lines)


# ---- MCP 能力探测：supports_mcp 得先问本机二进制才能回答的 CLI -------------------
#
# 多数 adapter 的 ``supports_mcp`` 是个常量：AICU 接线了就是 True。pi 是唯一的例外 ——
# 它的 MCP 入口**取决于本机装没装那个扩展**，写常量会说假话：
#
#   pi —— pi 本体没有 MCP。0.84.2 的 ``dist/`` 里 ``mcpServers`` 命中数为 0，
#         配置是由外部扩展 pi-mcp-adapter 读的（``pi install npm:pi-mcp-adapter``）。
#         扩展没装时 ``.pi/mcp.json`` 是个死文件。
#
# 这层探测的门槛得拿捏：它探的必须是**本机的能力**，而不是「CLI 有没有某个子命令」。
# kimi 曾经也挂在这里，探的正是后者（``kimi --help`` 里有没有 ``mcp``），而那个子命令
# 0.43.1 与 2.0.0 都没有 —— 门禁于是恒为假，把一条本来能用的链路整个挡掉。真正挡着
# 项目级配置的是 workspace trust，那是 AICU 自己就能写的（见 KimiAdapter）。
#
# 门禁判否时的对外表现与「还没接线」完全一致（守卫照常报错、前端标未接入）——
# 这正是 ``supports_mcp`` 注释里那条原则：**宁可报错说没接线，也不要写一个不生效的
# 文件还让用户以为配好了。**
_MCP_CAPABILITY: dict = {}
_MCP_CAPABILITY_LOCK = threading.Lock()


def _probe_stdout(argv: list, timeout: int = 15) -> Optional[str]:
    """跑一条探测命令并把 stdout+stderr 一起返回；跑不起来一律返回 None。

    两条流合并是有意的：CLI 报「不认这个子命令」时走 stderr 还是 stdout 各家不同
    （kimi 0.43.1 的 ``unknown command 'mcp'`` 就未必在 stdout），而探测要问的问题
    只有一个 ——「这段输出里有没有那个东西」。

    ``errors="replace"`` 不能省：默认的严格解码遇到非 UTF-8 字节会抛
    ``UnicodeDecodeError``，而它**既不是 OSError 也不是 SubprocessError**，会直接穿过
    下面的 except、再穿过 ``_mcp_capability`` 与 ``supports_mcp`` 一路冒到
    ``/v1/config`` 上去（设置页打不开）。探测的设计是「探不出来就判否」，不该有一条
    路径是「探不出来就炸」；换成替换解码还有个好处：CLI 只是往 help 里混了一个坏字节
    时，剩下的输出照样能判。
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        # 二进制不在、没权限、卡住超时 —— 一律当作「探测不出来」，也就是判否
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def _mcp_capability(command: str, probe) -> bool:
    """跑一次 ``probe`` 并按**解析后的二进制路径**缓存结果。

    键取路径而不是命令名：同一个 CLI 完全可能装着多个版本（npm 全局一份、nvm 或
    项目本地又一份），按名字缓存会把 A 版本上探到的结果用到 B 版本上 —— 而这两个
    版本的 MCP 能力差异正是要区分的东西。

    加锁是必需的：``mcp_enabled_clis()`` 会在 Web 请求线程里挨个读这些属性，
    而首次访问要起子进程。
    """
    path = shutil.which(command) or command
    with _MCP_CAPABILITY_LOCK:
        if path not in _MCP_CAPABILITY:
            _MCP_CAPABILITY[path] = probe(path)
        return _MCP_CAPABILITY[path]


def _probe_pi_mcp(command: str) -> bool:
    """``pi list`` 里有没有 pi-mcp-adapter —— 有扩展才谈得上收 MCP 配置。

    查的是**全局**安装（``pi list`` 不带 ``-l`` 读全局设置）。项目级安装对 AICU 没有
    意义：每次运行的 workspace 都是新生成的，装在里面的扩展下次就没了。
    """
    output = _probe_stdout([command, "list"])
    return output is not None and "pi-mcp-adapter" in output


# ---------------------------------------------------------------------------
# kimi 的 workspace 信任记录
#
# kimi 引擎加载 MCP 分两层（``loadMcpServers``）：
#   user 层    ``$KIMI_CODE_HOME/mcp.json``    —— 无条件加载
#   project 层 ``<cwd>/.kimi-code/mcp.json``   —— 只在 ``trust.isTrusted()`` 为真时加载
# 而 trust 只能由交互式 TUI 的授权提示授予，``kimi -p`` 永远走不到那一步 —— 所以 AICU
# 落一份 project 层配置却不补信任记录，等于什么都没做（这正是 2026-09-17 那次 A/B 里
# project 臂全 0 的原因）。
#
# 好在这个 trust 是**纯落盘记录**：``readWorkspaceTrust`` 的判据是
# ``docs.get("workspace-trust", trustKey(root)) !== void 0`` —— 只认 key 在不在，
# 内容不校验、不签名、不看时间。于是 AICU 可以把这条记录直接写出来。
#
# 下面两个函数逐行复刻 kimi 的 ``canonicalWorkspaceRoot`` + ``encodeWorkDirKey``，
# 改任何一处都要同步改这里，否则算出来的 key 与 kimi 查的那个对不上 —— 而那种失败是
# **静默的**：记录写了、没人读、MCP 不加载、全程没有任何报错。
# 黄金样本（本机真机记录，可用来校验算法是否还准）：
#   <repo root>/final_solution → wd_final_solution_5b262f6d586a（key 里带的是目录名与路径哈希）
# ---------------------------------------------------------------------------

# 与 kimi 的 ``TRUST_SCOPE`` 常量逐字一致，是 home 下的子目录名
_KIMI_TRUST_SCOPE = "workspace-trust"
# ``slugifyWorkDirName`` 里的 MAX_WORKDIR_SLUG_LENGTH；截断之后还要再剥一次首尾短横线
_KIMI_SLUG_MAX = 40


def _kimi_workspace_root(workspace) -> str:
    """kimi 眼里的 workspace 根 —— 对齐 Node 的 ``path.resolve`` + ``workspaceRootKey``。

    **必须用 ``realpath`` 而不是 ``abspath``**：key 的输入是子进程的 ``process.cwd()``，
    而 ``getcwd()`` 返回的是**物理路径**（软链已被化解），Node 的 ``path.resolve`` 又不会
    再解析一次 —— 所以含软链的 workspace 上两者算出的 key 不同。macOS 的
    ``/tmp → /private/tmp`` 就是现成例子。

    2026-09-18 三臂对照实测（唯一变量 = 记录怎么写）：不写记录全 0、abspath 写的全 0、
    realpath 写的 拉起=1 握手=1 tools/list=1 真调用=1。**这不是推理出来的，是测出来的。**
    """
    return os.path.realpath(str(workspace)).replace("\\", "/").rstrip("/")


def _kimi_trust_key(workspace) -> str:
    """``wd_<目录名小写化≤40>_<sha256(规范化路径)[:12]>``，与 kimi 的 ``encodeWorkDirKey`` 同形。"""
    root = _kimi_workspace_root(workspace)
    name = root.split("/")[-1] or root
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-")[:_KIMI_SLUG_MAX].strip("-")
    digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:12]
    return f"wd_{slug or 'workspace'}_{digest}"


def _write_kimi_trust(workspace) -> Optional[Path]:
    """写一条 workspace 信任记录并返回路径；**已经存在则返回 None**。

    返回 None 那一条是有意的：记录已经在那儿，说明用户自己在 TUI 里授过信、或者上一次
    AICU 运行留下的 —— 两种情况都不该由本次运行删掉。调用方按「返回了才删」处理
    （见 ``CliAdapter.mcp_workspace_files`` 的返回值契约）。

    home 按 ``resolveKimiHome`` 的同序取：``KIMI_CODE_HOME`` 优先，否则 ``~/.kimi-code``。
    跟着这个变量走是必须的 —— kimi 从同一个 home 读 mcp.json 与信任记录，写岔了等于没写。
    """
    home = os.environ.get("KIMI_CODE_HOME") or str(Path.home() / ".kimi-code")
    record = Path(home).joinpath(_KIMI_TRUST_SCOPE, _kimi_trust_key(workspace))
    if record.exists():
        return None
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        # 紧凑 JSON、无尾换行、trustedAt 毫秒 int —— 与 kimi 自己写的那条逐字同形
        record.write_text(
            json.dumps({"root": _kimi_workspace_root(workspace),
                        "trustedAt": int(time.time() * 1000)},
                       separators=(",", ":")),
            encoding="utf-8")
    except OSError:
        # 写不进去（home 只读 / 权限不足）不该让整次运行失败，退化成「MCP 不生效」，
        # 与改动前一致。不吞的话，一个可选的增强会变成硬故障。
        return None
    return record


def _write_pi_trust(workspace) -> Optional[Path]:
    """写一条 pi 的 workspace 信任记录并返回路径；**已经是 true 则返回 None**。

    为什么需要它 —— 源码级证据在 ``dist/core/project-trust.js`` 的 resolveProjectTrusted：

        if (options.trustOverride !== undefined) return options.trustOverride;
        if (!hasTrustRequiringProjectResources(cwd)) return true;
        const decision = trustStore.get(cwd);      // 查 trust.json 里最近的一条
        if (decision !== null) return decision;
        switch (options.defaultProjectTrust ?? "ask") { ... }
        if (!options.projectTrustContext.hasUI) return false;   // ← 无头模式走到这里

    而 ``hasTrustRequiringProjectResources``（同上目录的 trust-manager.js）看的是
    ``<cwd>/.pi/<entry>``，entry 来自 ``TRUST_REQUIRING_PROJECT_CONFIG_RESOURCES`` ——
    里面**明写着 "skills"**。也就是说：项目里一有 ``.pi/skills/``，这个项目就变成
    「需要授信」；AICU 跑的 ``pi -p`` 没有 UI，于是 ``hasUI`` 为假、直接返回 false，
    **项目技能一个都不加载**。

    实测与此吻合（2026-09-20，同一份 ``.pi/skills/`` 软链，只差这条记录）：
      没有记录 → A 组 8 轮全空，连内置清单都没列出来（正是「整个项目层被跳过」的样子）
      有记录   → A 组答出金丝雀，B 组没有

    ``defaultProjectTrust`` 那个设置项能把默认值改成 always，但它是**用户级**的、
    且会作用于用户的所有项目 —— 为了跑通 AICU 去改用户的安全默认值，不是我们该做的事。
    按项目写一条精确的记录才是等价的、更窄的动作。

    顺带解释了「为什么这个问题一直没被发现」：受信清单里**没有 mcp.json**，所以 pi 的
    MCP 一路都正常，只有 skill 撞上这道门禁 —— 而它撞上之后的症状是「目录建了、
    ``ls`` 看着对、永远不生效」。

    信任库是 ``<agent dir>/trust.json``，形状 ``{"<canonical 绝对路径>": true}``；
    判定时从 cwd 一路往上找最近的一条（``findNearestTrustEntry``）。

    agent dir 按 ``getAgentDir`` 的同序取：``PI_CODING_AGENT_DIR`` 优先，否则
    ``~/.pi/agent``。跟着这个变量走是必须的 —— pi 从同一个目录读 settings 与信任记录。

    与 ``_write_kimi_trust`` 的两处差别，都是有意的：
      - **不删**。kimi 那条跟着一次性 MCP 配置被删掉；skill 是长期留存的，删了下次
        运行还得再写一遍，而它必须在 CLI 启动前就在。代价是 ``trust.json`` 会随运行
        次数攒下条目 —— 每条形如一行 ``"<workspace>": true``，且指向的是 AICU 自己的
        workspace，不是用户的项目。
      - **不认识的文件不动**。``trust.json`` 是 pi 自己的文件（用户可能手工编辑过、
        也可能来自更新的 pi 版本）。读不出 dict 就整体放弃，绝不覆盖 —— 它是用户的
        数据，我们只是往里加一行。
    """
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR") or str(Path.home() / ".pi" / "agent")
    trust_path = Path(agent_dir) / "trust.json"
    # pi 的 canonicalizePath 就是 realpathSync：解软链再比。AICU 给的 workspace 本来
    # 就是 resolve 过的，这里再 resolve 一次是为了让「探针直接传原串」也成立。
    key = os.path.realpath(str(workspace))
    try:
        data = {}
        if trust_path.exists():
            data = json.loads(trust_path.read_text(encoding="utf-8") or "{}")
            if not isinstance(data, dict):
                return None
        if data.get(key) is True:
            return None
        data[key] = True
        trust_path.parent.mkdir(parents=True, exist_ok=True)
        # 缩进 2 + 尾换行 —— 与 pi 自己 writeTrustFile 的写法同形（它还会按键排序，
        # 这里不排：键的先后 pi 不关心，而排序会把用户的原有顺序也一并改掉）
        trust_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    except (OSError, ValueError):
        # 与 _write_kimi_trust 同一条纪律：写不进去不该让整次运行失败，退化成
        # 「skill 不生效」（用户会收到 AICU 的告警），而不是变成一个硬故障。
        return None
    return trust_path


def _mcp_servers_settings_json(servers: list) -> str:
    """qwen 项目级 ``.qwen/settings.json`` 里的 ``mcpServers`` 段。

    与 ``_cc_mcp_json`` 只差一个 ``trust``：本地形状同样是 ``command`` 字符串 +
    ``args`` 数组（**不是** opencode 那种数组合一形式），但必须带 ``trust: true``。

    ``trust`` 不是可选项。qwen 把项目级 server 默认置为 "Pending approval" 状态
    （``qwen mcp list`` 实测），而 AICU 跑的是无头模式 —— 没有人在旁边点批准，
    不写 trust 等于下发了一个永远批不下来的工具。``trust`` 的语义是「在受信任的
    工作区里跳过该 server 的工具调用确认」，与 ``--yolo`` 是两回事。

    形状按 qwen 自己 ``mcp add -s project`` 写出来的文件对齐（已实测比对）。

    远程条目用 **``httpUrl``**，不是 ``url`` —— qwen 没有 type 字段，传输**由键名决定**：
    ``url`` 表示 SSE（已被 Streamable HTTP 取代的旧传输），``httpUrl`` 才是 Streamable
    HTTP。写成 ``url`` 不会报错，只会静默连到一条我们并不支持的传输上，然后以一句与
    传输无关的超时告终。``trust`` 在远程条目上同样要写（批准门禁与传输无关）。
    """
    entries: dict = {}
    for server in servers:
        if server.transport == TRANSPORT_HTTP:
            entries[server.name] = {**_remote_fields(server, "httpUrl"), "trust": True}
            continue
        entry: dict = {
            "command": server.command,
            "args": list(server.args),
            "trust": True,
        }
        if server.env:
            entry["env"] = dict(server.env)
        entries[server.name] = entry
    return json.dumps({"mcpServers": entries}, ensure_ascii=False, indent=2) + "\n"
