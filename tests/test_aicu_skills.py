"""skills 按 CLI 分发到各自项目级目录的测试。

背景：``skills:`` 曾经只在 claude 系生效 —— ``_install_skills_to_cc`` 把
``<workspace>/.claude/skills`` **写死**、对所有 CLI 无条件地建软链。其余 CLI 上那是
一堆死文件：建了、``ls`` 看着对、永远不生效，而用户以为自己配好了。

本文件守三件事：
  1. **落到对的地方** —— 每个 adapter 声明的 ``skills_workspace_dir`` 下真的建出软链，
     且指回源目录（不是拷贝、不是空目录）。
  2. **接不上时的行为** —— 只告警、照常跑，且**没配 skills 的用户一个字都看不到**。
  3. **不污染交付物** —— SKILL.md 不会被产物分类当成 CC 写的代码。

诚实说明本文件的边界：目录建对了**不等于** CLI 真的会加载它。路径对不对是各 CLI 的
内部事实，只有真机验证能证明（见 /tmp/skillprobe 那套 A/B）。这里守的是**接线是通的**。
"""

import json
import os

import pytest

import cli_adapters
from aicu import AICU

SKILL_NAME = "probe-skill"


# ============================================================================
# 构造助手
# ============================================================================


def _make_skill_source(tmp_path, names=(SKILL_NAME,)):
    """真的建一份 skill 源目录：软链接指向的 src 必须存在。"""
    src = tmp_path / "src-skills"
    for name in names:
        skill = src / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: 探针技能\n---\n\n正文。\n",
            encoding="utf-8",
        )
    return src


def _make_aicu(tmp_path, *, cli_name, skills, skills_dir, role="code", num_ccs=1):
    """离线构造一个 AICU（不 chat，只走构造期）。

    与 ``test_mcp_capability_note._make_aicu`` 同形 —— 两个文件都在构造同一套
    「真实 Session Shared Workspace 布局」，改动其一记得看另一个。
    """
    base = tmp_path / f"workspace-{cli_name}"
    node_ws = base / "nodes" / "n1"
    os.makedirs(node_ws, exist_ok=True)
    return AICU(
        role_type="worker",
        role=role,
        parent_workspace=str(node_ws),
        num_ccs=num_ccs,
        cli_name=cli_name,
        skills=list(skills),
        skills_dir=str(skills_dir),
        modular_output=True,
        output_file=None,
    )


@pytest.fixture(autouse=True)
def _pretend_installed(monkeypatch):
    """本文件只关心 skills 怎么下发，不关心本机装没装那个 CLI。

    不拨这一下，用例的覆盖面跟着开发机走 —— 没装 qwen 的机器上那条直接构造失败，
    而它在有 qwen 的机器上是绿的，两条都没在验「分发逻辑」。（同
    TestMcpWiringGuard._pretend_installed 的写法。）
    """
    monkeypatch.setattr(cli_adapters.CliAdapter, "is_installed", lambda self: True)


def _declared_clis():
    """(cli_id, 期望的相对目录) 列表 —— 从 adapter 派生，不写死名单。"""
    return [
        (adapter.id, adapter.skills_workspace_dir)
        for adapter in cli_adapters._ADAPTERS
        if adapter.skills_workspace_dir is not None
    ]


def _declared_dirnames():
    """所有会被写到的顶层目录名，去重后排序 —— 同样从 adapter 派生。

    写死名单的版本会在有人新接一家 CLI 时**静静地漏测**：新目录不在参数里，
    产物分类那条用例照旧绿，而被漏掉的那个目录正是会漏进交付物的那个。
    """
    return sorted({d[0] for _, d in _declared_clis()})


# ============================================================================
# 一、落到对的地方
# ============================================================================


class TestSkillsLandInTheRightDirectory:
    @pytest.mark.parametrize("cli_id,expected", _declared_clis())
    def test_symlink_created_in_the_cli_own_directory(self, tmp_path, cli_id, expected):
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name=cli_id, skills=[SKILL_NAME], skills_dir=src)

        assert aicu.ccs, "没造出 CC"
        for cc in aicu.ccs:
            link = cc.workspace.joinpath(*expected, SKILL_NAME)
            assert link.is_symlink(), (
                f"{cli_id} 的 skill 没落到 {('/'.join(expected))}/ —— "
                f"声明了 skills_workspace_dir 却没建，等于静默失效"
            )
            # 指回源目录（而不是被拷了一份、或指向一个不存在的地方）
            assert link.resolve() == (src / SKILL_NAME).resolve()

    def test_claude_and_internal_keep_the_historical_path(self, tmp_path):
        """claude / internal 是唯一**本来就已生效**的两家，路径不能变 —— 它今天就在 .claude/skills。"""
        for cli_id in ("claude", "internal"):
            src = _make_skill_source(tmp_path / cli_id)
            aicu = _make_aicu(
                tmp_path / cli_id, cli_name=cli_id, skills=[SKILL_NAME], skills_dir=src
            )
            assert (aicu.ccs[0].workspace / ".claude" / "skills" / SKILL_NAME).is_symlink()

    def test_codebuddy_does_not_inherit_the_parent_directory(self, tmp_path):
        """codebuddy 复用 ClaudeCodeAdapter，但目录**不一样**。

        这是本文件最该守住的一条：三个 CLI 共用一个类，只要有人把子类这行覆盖删掉，
        代码照样跑、目录照样建、`ls` 照样好看 —— 只是 codebuddy 上永远不生效。
        真机 A/B 实测：``.claude/skills`` 时模型答 NONE，``.codebuddy/skills`` 时答出金丝雀。
        """
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(
            tmp_path, cli_name="codebuddy", skills=[SKILL_NAME], skills_dir=src
        )
        workspace = aicu.ccs[0].workspace
        assert (workspace / ".codebuddy" / "skills" / SKILL_NAME).is_symlink()
        assert not (workspace / ".claude").exists(), (
            "codebuddy 不该再有 .claude/skills —— 那是它读不到的目录"
        )

    def test_non_claude_clis_do_not_get_a_dead_dot_claude_directory(self, tmp_path):
        """修复前那个坏法的正面反例：别家**不该**再有那份没人读的 .claude/skills。"""
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name="qwen", skills=[SKILL_NAME], skills_dir=src)
        assert not (aicu.ccs[0].workspace / ".claude").exists()

    def test_kimi_uses_the_agents_directory_not_claude(self, tmp_path):
        """kimi 曾经报 ``.claude/skills``（理由是「与 claude 系同目录、少维护一个路径」）。

        2026-09-20 真机 A/B 证伪：同一份软链，``.claude/skills`` 时 kimi 清单里只有
        Built-in，``.agents/skills`` 时多出 Project 一节、金丝雀原样答出。
        """
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name="kimi", skills=[SKILL_NAME], skills_dir=src)
        workspace = aicu.ccs[0].workspace
        assert (workspace / ".agents" / "skills" / SKILL_NAME).is_symlink()
        assert not (workspace / ".claude").exists()

    def test_multiple_skills_and_multiple_ccs(self, tmp_path):
        names = ("alpha-skill", "beta-skill")
        src = _make_skill_source(tmp_path, names)
        aicu = _make_aicu(
            tmp_path, cli_name="pi", skills=list(names), skills_dir=src, num_ccs=3
        )
        assert len(aicu.ccs) == 3
        for cc in aicu.ccs:
            for name in names:
                assert (cc.workspace / ".pi" / "skills" / name).is_symlink()

    def test_reinstall_is_idempotent(self, tmp_path):
        """软链目录是清空重建的 —— 重复安装不能叠加、不能抛。"""
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name="qwen", skills=[SKILL_NAME], skills_dir=src)
        cc = aicu.ccs[0]
        aicu._install_skills_to_cc(cc)  # 再来一次
        target = cc.workspace / ".qwen" / "skills"
        assert [p.name for p in target.iterdir()] == [SKILL_NAME]


# ============================================================================
# 二、接不上的 CLI：只告警、照常跑
# ============================================================================


class TestUnwiredCliWarnsButRuns:
    def test_warns_once_and_still_constructs(self, tmp_path, capsys):
        """接不上就只告警 —— 与 MCP 的硬门禁刻意不同（那边的失败是「工具根本不存在」）。"""
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(
            tmp_path, cli_name="grok", skills=[SKILL_NAME], skills_dir=src, num_ccs=3
        )
        assert aicu.ccs, "接不上 skills 不该阻止构造"

        out = capsys.readouterr().out
        assert "警告" in out
        assert "grok" in out
        assert SKILL_NAME in out
        # 三个 CC，但只该说一次
        assert out.count("警告") == 1

    def test_nothing_is_written_for_an_unwired_cli(self, tmp_path, capsys):
        """告警之外不得在盘上留下任何东西 —— 半个目录比没有目录更难查。"""
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name="grok", skills=[SKILL_NAME], skills_dir=src)
        before = set(aicu.ccs[0].workspace.rglob("*"))
        assert not any(p.is_symlink() for p in before)

    def test_reason_phrase_comes_from_the_adapter(self, tmp_path, capsys, monkeypatch):
        """成因由 adapter 给，守卫里不写死 —— pi 那种「装个扩展就行」的 CLI 会需要它。"""
        monkeypatch.setattr(
            cli_adapters.GrokAdapter,
            "skills_unwired_reason",
            lambda self: "测试用的原因短语",
        )
        src = _make_skill_source(tmp_path)
        _make_aicu(tmp_path, cli_name="grok", skills=[SKILL_NAME], skills_dir=src)
        assert "测试用的原因短语" in capsys.readouterr().out


# ============================================================================
# 三、没配 skills 的用户一个字都看不到
# ============================================================================


class TestNoSkillsIsCompletelySilent:
    def test_empty_skills_produces_no_output(self, tmp_path, capsys):
        """绝大多数角色是 ``skills: []`` —— 它们的日志里不该多出任何东西。"""
        src = _make_skill_source(tmp_path)
        _make_aicu(tmp_path, cli_name="grok", skills=[], skills_dir=src)
        # AICU 的初始化日志里本来就会打印临时路径，而 pytest 的目录名带着用例名
        # （里面就有 "skills"）—— 先把路径抹掉再断言，否则测的是临时目录的命名。
        out = capsys.readouterr().out.replace(str(tmp_path), "<tmp>")
        assert "skill" not in out.lower()
        assert "警告" not in out

    def test_empty_skills_creates_no_directory(self, tmp_path):
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name="qwen", skills=[], skills_dir=src)
        assert not (aicu.ccs[0].workspace / ".qwen").exists()


# ============================================================================
# 四、产物分类：SKILL.md 不是 CC 写的代码
# ============================================================================


class TestSkillFilesAreNotDeliverables:
    """``deliverable_dir`` 默认为 None ⇒ 交付目录就是 workspace 根，收集会扫到 skills 目录。

    SKILL.md 是 ``.md``（不在 NONCODE_EXTENSIONS 里）、也不在 NONCODE_FILENAMES 里，
    所以只能靠 NONCODE_DIRS 挡 —— 不挡的话它会被复制进最优方案目录、并出现在 _INDEX.md。
    """

    @pytest.mark.parametrize("dirname", _declared_dirnames())
    def test_skill_md_under_a_cli_dir_is_not_code(self, tmp_path, dirname):
        # 必须真建出这个文件：_is_code_file 第一句就是 ``if not f.is_file(): return False``，
        # 拿一个不存在的路径去问，它恒返回 False —— 那种断言恒真，什么都没测。
        skill_md = tmp_path / dirname / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True, exist_ok=True)
        skill_md.write_text("---\nname: x\n---\n", encoding="utf-8")

        aicu = _make_aicu(
            tmp_path,
            cli_name="qwen",
            skills=[],
            skills_dir=_make_skill_source(tmp_path),
        )
        assert not aicu._is_code_file(skill_md, tmp_path)

    def test_an_ordinary_markdown_file_is_still_code(self, tmp_path):
        """对照组：普通目录下的 .md 仍然算代码 —— 上一条不能是靠「.md 一律不算」蒙对的。"""
        notes = tmp_path / "deliverables" / "notes.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text("# 交付说明\n", encoding="utf-8")

        aicu = _make_aicu(
            tmp_path, cli_name="qwen", skills=[], skills_dir=_make_skill_source(tmp_path)
        )
        assert aicu._is_code_file(notes, tmp_path)

    def test_ancestor_dir_named_like_noncode_dir_does_not_hide_files(self, tmp_path):
        """工作区的祖先目录恰好叫 build / env 时，里面的产出不能被整棵判成非代码。"""
        root = tmp_path / "build" / "env" / "ws"
        notes = root / "report.md"
        skill_md = root / ".qwen" / "skills" / "x" / "SKILL.md"
        for f in (notes, skill_md):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# x\n", encoding="utf-8")
        aicu = _make_aicu(
            tmp_path, cli_name="qwen", skills=[], skills_dir=_make_skill_source(tmp_path)
        )
        assert aicu._is_code_file(notes, root)
        assert not aicu._is_code_file(skill_md, root)  # 相对 root 的排除仍生效
        assert aicu._list_code_files_in_dir(root) == [notes]


# ============================================================================
# 五、项目信任记录：目录摆对了，CLI 还可能要一份「这个项目我信」的记录
# ============================================================================


class TestProjectTrustRecords:
    """有的 CLI 把项目级 skill 关在**项目信任门禁**后面，光摆目录永远不生效。

    真机 A/B 实测（2026-09-20，同一份 ``.pi/skills/`` 软链，只差这条记录）：

        pi  无记录 → A、B 两组都只有内置清单，模型答 NONE
        pi  有记录 → A 组答出金丝雀，B 组没有

    这正是本任务要消灭的失效模式，而且是最难查的一种：目录建了、``ls`` 看着对、
    code review 也挑不出毛病，只是永远不生效。无头模式（``pi -p``）永远等不到 TUI 的
    授权提示，所以这份信任只能由 AICU 写。
    """

    @pytest.fixture(autouse=True)
    def _isolated_agent_dir(self, tmp_path, monkeypatch):
        """把 pi 的 agent dir 钉到 tmp —— 绝不碰真实 ``~/.pi/agent/trust.json``。

        走 ``PI_CODING_AGENT_DIR``（pi 自己的 ``getAgentDir`` 就认它），不是直接改
        ``Path.home()``：两者都隔离，但前者才是被测代码真正读的那个变量。
        """
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))

    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path, monkeypatch):
        """再隔离一次 ``Path.home()`` —— 构造 pi 的 agent 会走 ``provision()``。

        同 ``test_claude_code_agent._isolated_home``：``_provision_pi_config`` /
        ``_release_pi_config`` 认的是 ``Path.home()/.pi/agent/models.json``，不隔离就会
        动到开发机上真实的 pi 配置。
        """
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path / "home")

    def _trust_path(self, tmp_path):
        return tmp_path / "pi-agent" / "trust.json"

    def _read(self, tmp_path) -> dict:
        return json.loads(self._trust_path(tmp_path).read_text(encoding="utf-8"))

    def test_every_cc_workspace_gets_a_record(self, tmp_path):
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(
            tmp_path, cli_name="pi", skills=[SKILL_NAME], skills_dir=src, num_ccs=2
        )
        data = self._read(tmp_path)
        for cc in aicu.ccs:
            key = os.path.realpath(str(cc.workspace))
            assert data.get(key) is True, f"{key} 不在信任记录里 —— pi 不会加载它的 skill"
        assert len(data) == 2

    def test_the_record_is_a_realpath_not_the_raw_string(self, tmp_path):
        """键必须是 realpath —— pi 的 ``canonicalizePath`` 就是 ``realpathSync``。

        macOS 的 ``/tmp`` 是 ``/private/tmp`` 的软链，两边写法都可能出现；写成非
        canonical 的那一种，pi 就查不到这条记录，**静默**不加载 skill。
        """
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name="pi", skills=[SKILL_NAME], skills_dir=src)
        cc = aicu.ccs[0]
        adapter = cli_adapters.get_adapter("pi")

        assert os.path.realpath(str(cc.workspace)) in self._read(tmp_path)
        # 已经授信过了 —— 再问一次不该再写
        assert adapter.skills_workspace_trust(cc.workspace) is None

    def test_existing_entries_are_preserved(self, tmp_path):
        """``trust.json`` 是 pi 自己的文件，用户可能手工编辑过 —— 我们只加一行。"""
        path = self._trust_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{\n  "/some/user/project": true,\n  "note": "手工加的"\n}\n', encoding="utf-8"
        )

        src = _make_skill_source(tmp_path)
        _make_aicu(tmp_path, cli_name="pi", skills=[SKILL_NAME], skills_dir=src)

        data = self._read(tmp_path)
        assert data["/some/user/project"] is True
        assert data["note"] == "手工加的"

    def test_an_unrecognized_file_is_not_overwritten(self, tmp_path):
        """读不出 dict 就整体放弃 —— 它可能是更新版 pi 的格式，覆盖等于毁用户数据。"""
        path = self._trust_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]\n", encoding="utf-8")

        adapter = cli_adapters.get_adapter("pi")
        assert adapter.skills_workspace_trust(tmp_path / "some-workspace") is None
        assert path.read_text(encoding="utf-8") == "[1, 2, 3]\n"

    def test_write_failure_degrades_instead_of_raising(self, tmp_path, monkeypatch):
        """写不进去不该让整次运行失败 —— 退化成「skill 不生效」，用户会收到告警。"""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(blocker))

        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(tmp_path, cli_name="pi", skills=[SKILL_NAME], skills_dir=src)
        assert aicu.ccs, "信任记录写不进去不该阻止构造"
        assert (aicu.ccs[0].workspace / ".pi" / "skills" / SKILL_NAME).is_symlink()

    def test_only_pi_declares_a_trust_hook(self, tmp_path):
        """别的 CLI 调这个钩子必须什么都不做 —— 免得有人「顺手」给每家都写一份。

        kimi 尤其值得说：它**有**项目信任门禁，但那道门禁卡的是 MCP
        （``loadMcpServers({includeProject: this.trust.isTrusted()})``），不卡 skill ——
        实测去掉信任记录后 kimi 仍然 PASS。所以这里不为它写。
        """
        for adapter in cli_adapters._ADAPTERS:
            if adapter.id == "pi":
                continue
            assert adapter.skills_workspace_trust(tmp_path / "ws") is None, adapter.id


# ============================================================================
# 六、既有校验一个字不改
# ============================================================================


class TestExistingValidationIsUnchanged:
    def test_missing_skills_dir_still_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="skills_dir 不存在"):
            _make_aicu(
                tmp_path,
                cli_name="qwen",
                skills=[SKILL_NAME],
                skills_dir=tmp_path / "nope",
            )

    def test_missing_skill_still_raises_and_lists_available(self, tmp_path):
        src = _make_skill_source(tmp_path, names=("alpha-skill",))
        with pytest.raises(FileNotFoundError) as info:
            _make_aicu(tmp_path, cli_name="qwen", skills=["ghost-skill"], skills_dir=src)
        message = str(info.value)
        assert "ghost-skill" in message
        assert "alpha-skill" in message  # 可用清单仍要列出来


# ============================================================================
# 「Skill 优先」提示块
# ============================================================================


def _capture_chat(aicu):
    """打在 cc.chat 这一层记录送出的 prompt；换掉 _cc_chat_with_retry 会绕过被测逻辑。"""
    sent = []
    for cc in aicu.ccs:
        cc.chat = lambda prompt, max_turns=None, timeout=7200, _s=sent: (_s.append(prompt) or "ok")
    return sent


class TestSkillsNote:
    NOTE_HEAD = "【Skill 优先】"

    def test_note_reaches_every_send_with_names_and_path(self, tmp_path):
        src = _make_skill_source(tmp_path)
        aicu = _make_aicu(
            tmp_path, cli_name="claude", skills=[SKILL_NAME], skills_dir=src
        )
        sent = _capture_chat(aicu)

        aicu._cc_chat_with_retry(aicu.ccs[0], "任务一")
        aicu._cc_chat_with_retry(aicu.ccs[0], "任务二")

        assert len(sent) == 2
        for prompt in sent:
            assert self.NOTE_HEAD in prompt
            assert SKILL_NAME in prompt
            assert ".claude/skills" in prompt
        assert sent[0].endswith("任务一")

    def test_path_follows_adapter_dir(self, tmp_path):
        from aicu import _render_skills_note

        note = _render_skills_note(["a", "b"], (".codebuddy", "skills"))
        assert ".codebuddy/skills/<skill 名>/SKILL.md" in note
        assert "a、b" in note

    def test_empty_when_not_applicable(self):
        from aicu import _render_skills_note

        assert _render_skills_note([], (".claude", "skills")) == ""
        assert _render_skills_note(["a"], None) == ""

    def test_no_half_width_braces(self):
        from aicu import _render_skills_note

        assert "{" not in _render_skills_note(["a"], (".claude", "skills"))
        assert "}" not in _render_skills_note(["a"], (".claude", "skills"))

    def test_no_skills_prompt_is_byte_identical(self, tmp_path):
        aicu = _make_aicu(
            tmp_path, cli_name="claude", skills=[], skills_dir=tmp_path / "none"
        )
        sent = _capture_chat(aicu)

        aicu._cc_chat_with_retry(aicu.ccs[0], "原始任务")

        assert sent[0] == f"{aicu.ccs[0]._workspace_boundary}\n\n原始任务"
