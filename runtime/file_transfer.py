"""把文件搬进/搬出「可浏览根」的纯逻辑：校验、改名、流式落盘。

为什么单独一个模块：这几件事的调用方是两条互不相干的路径 —— 会话共享区（``shared``）
与节点产出目录（``results``）。放进 ``runtime.py`` 会随 Runtime 一起被导入；放进
``shared_io.py`` 也不合适，那个模块的边界是「shared 根」，而这里的每个函数对根一无所知
（根由调用方给）。测试因此可以只 import 这一个模块，不建 Runtime、不起 app。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import secrets
import threading
from typing import Any, AsyncIterator

# 单文件上限。参照物：``skills_manager._MAX_IMPORT_BYTES`` 是 20MB（zip 归档），
# ``Runtime.MAX_PREVIEW_BYTES`` 是 1MB（文本预览）—— 都不是同类量级，不能直接照搬。
# revise 时递的「参考资料」可能是一整份数据集，100MB 是「够用、又不至于把常驻服务拖垮」的取值。
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# macOS APFS 与 Linux ext4 的 NAME_MAX 都是 255 **字节**，不是字符 ——
# 中文名 86 个字就顶到上限了。按字符判会放过它，然后在 open() 那里炸成
# ``OSError: File name too long``，也就是一个 500。
MAX_NAME_BYTES = 255


class UploadTooLarge(ValueError):
    """超过 ``MAX_UPLOAD_BYTES``。

    继承 ValueError 是为了让不认识它的调用方仍然按「参数问题」处理；路由层必须把
    ``except UploadTooLarge`` 写在 ``except ValueError`` **前面**，否则永远走不到。
    """


class RevealUnsupported(RuntimeError):
    """当前平台没有「在文件管理器中显示」这条能力，与请求本身无关 —— 所以不是 ValueError。"""


def validate_upload_name(name: str) -> str:
    """校验一个**单组件**文件名，通过则原样返回，否则抛 ValueError。

    不替换非法字符、不静默改名字：用户拖进去一个 ``a:b.txt``，得到一个悄悄变成
    ``a_b.txt`` 的文件，比收到一句「文件名非法」困惑得多。
    """
    if name in ("", ".", ".."):
        raise ValueError(f"Invalid file name: {name!r}")
    # 单组件约束。这一条之后穿越在结构上就不可能了，调用方仍会再做一次
    # realpath containment —— 那是纵深防御，不是重复。
    if "/" in name or "\\" in name or os.sep in name:
        raise ValueError(f"File name must not contain a path separator: {name!r}")
    # 控制字符：NUL 会截断路径，\n\r 会污染日志。
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"File name must not contain control characters: {name!r}")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(
            f"File name is longer than {MAX_NAME_BYTES} bytes: {name!r}"
        )
    return name


def unique_destination(directory: str, name: str) -> tuple[str, bool]:
    """在 ``directory`` 里给 ``name`` 找一个不冲突的名字，返回 ``(最终名, 是否改过)``。

    同名一律 ``a.txt`` → ``a-1.txt`` → ``a-2.txt``，**绝不覆盖** —— 覆盖是这里唯一
    不可接受的失败：用户拖进来的可能是他手上唯一的那份。

    用 ``os.path.lexists`` 而不是 ``exists``：目录同名要跳过（``os.replace`` 到目录上会炸），
    悬空软链也要跳过（``exists`` 对它返回 False，于是我们会把软链本身覆盖掉）。
    """
    if not os.path.lexists(os.path.join(directory, name)):
        return name, False
    stem, ext = os.path.splitext(name)
    for index in itertools.count(1):
        candidate = f"{stem}-{index}{ext}"
        if len(candidate.encode("utf-8")) > MAX_NAME_BYTES:
            # 原名已经贴着上限时，「-1」这两个字节就放不下了。宁可直接报错，也不要
            # 截断 stem —— 悄悄少几个字的名字比一句明确的失败更难排查。
            raise ValueError(
                f"Cannot find a non-conflicting name for {name!r} within "
                f"{MAX_NAME_BYTES} bytes"
            )
        if not os.path.lexists(os.path.join(directory, candidate)):
            return candidate, True


# 「改名的判断」与「落盘」必须原子。前端是并发上传（3 路），两个同名请求完全可能
# 同时算出 ``a-1.txt``，后 replace 的覆盖先落的 —— 那就直接违反了上面那条硬约束。
# 临界区只有 lexists + os.replace，全局一把锁的代价可以忽略。
_RENAME_LOCK = threading.Lock()


async def receive_upload(
    target_dir: str,
    name: str,
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """把 ``chunks`` 流式写进 ``target_dir``，返回 ``{"name", "path", "size", "renamed"}``。

    ``target_dir`` **必须已经由调用方校验过**（在可浏览根内、且是个目录）—— 这个函数
    对「根」一无所知，只负责「怎么落」。``name`` 是最终文件名（可能被改名过），
    ``path`` 是它在 ``target_dir`` 下的相对路径。

    先写 ``.aicu-upload-*.part`` 再 ``os.replace``：shared 根是 agent 正在读的目录，
    直接原地写会留下一个「名字已就位、内容是半截」的窗口，agent 完全可能读到一个
    语法不完整的 JSON。同时，这也是超限/中断时不留下半个文件的保证。
    """
    # 默认值在这里取而不是写在签名上：写成 `max_bytes=MAX_UPLOAD_BYTES` 会在 def 执行
    # 那一刻把常量**绑定死**，之后再改模块常量（测试里就是这么干的）对它毫无影响。
    if max_bytes is None:
        max_bytes = MAX_UPLOAD_BYTES
    # 临时名不带原始文件名：原名可能已经贴着 NAME_MAX，再挂后缀就溢出了；
    # 而且并发上传时两个请求的临时名必须互不相同，否则它们会互相截断。
    part_path = os.path.join(
        target_dir, f".aicu-upload-{os.getpid()}-{secrets.token_hex(8)}.part"
    )
    written = 0
    loop = asyncio.get_running_loop()
    try:
        with open(part_path, "wb") as handle:
            async for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise UploadTooLarge(
                        f"File exceeds the {max_bytes} byte upload limit"
                    )
                # 本进程同时跑 SSE 与 PTY WebSocket，在事件循环里同步写大文件
                # 会让终端卡住。用 stdlib 的 executor 而不是 starlette 的
                # run_in_threadpool —— 后者会把 web 框架拖进 runtime 层的 import 图。
                await loop.run_in_executor(None, handle.write, chunk)
        with _RENAME_LOCK:
            final_name, renamed = unique_destination(target_dir, name)
            os.replace(part_path, os.path.join(target_dir, final_name))
    except BaseException:
        # 失败/超限/取消都不留半个文件。
        try:
            os.unlink(part_path)
        except OSError:
            pass
        raise
    return {
        "name": final_name,
        "path": final_name,
        "size": written,
        "renamed": renamed,
    }
