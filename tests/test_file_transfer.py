"""runtime/file_transfer.py 单元测试。

全部是纯函数（或只碰 tmp_path 的函数），不建 Runtime、不起 app —— 这个模块的边界
就是「对根一无所知」，测试照着这个边界写才能钉住它。
"""

import asyncio
import os

import pytest

from runtime.file_transfer import (
    MAX_NAME_BYTES,
    UploadTooLarge,
    receive_upload,
    unique_destination,
    validate_upload_name,
)


def _chunks(payload: bytes, size: int = 4):
    async def generator():
        for start in range(0, len(payload), size):
            yield payload[start:start + size]

    return generator()


class TestValidateUploadName:
    def test_accepts_ordinary_names(self):
        for name in ["a.txt", "中文名.pdf", "with space.md", ".env", "a-b_c.tar.gz"]:
            assert validate_upload_name(name) == name

    @pytest.mark.parametrize("bad", ["", ".", ".."])
    def test_rejects_empty_and_dot_names(self, bad):
        with pytest.raises(ValueError):
            validate_upload_name(bad)

    @pytest.mark.parametrize("bad", ["a/b.txt", "a\\b.txt", "../a.txt", "/etc/passwd"])
    def test_rejects_path_separators(self, bad):
        """单组件约束 —— 这一条之后穿越在结构上就不可能了。"""
        with pytest.raises(ValueError):
            validate_upload_name(bad)

    @pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\rb", "a\x7fb"])
    def test_rejects_control_characters(self, bad):
        with pytest.raises(ValueError):
            validate_upload_name(bad)

    def test_length_limit_is_in_bytes_not_characters(self):
        """255 是 NAME_MAX 的**字节**数。按字符判会放过中文名，然后在 open() 炸成 500。"""
        assert len("中" * 85) == 85 and len("中" * 85) < MAX_NAME_BYTES
        validate_upload_name("中" * 85)  # 255 字节，正好通过
        with pytest.raises(ValueError):
            validate_upload_name("中" * 86)  # 258 字节


class TestUniqueDestination:
    def test_free_name_is_untouched(self, tmp_path):
        assert unique_destination(str(tmp_path), "a.txt") == ("a.txt", False)

    def test_appends_suffix_without_touching_the_original(self, tmp_path):
        (tmp_path / "a.txt").write_text("original", encoding="utf-8")
        assert unique_destination(str(tmp_path), "a.txt") == ("a-1.txt", True)
        (tmp_path / "a-1.txt").write_text("second", encoding="utf-8")
        assert unique_destination(str(tmp_path), "a.txt") == ("a-2.txt", True)
        # 原文件一个字节都没动 —— 覆盖是这里唯一不可接受的失败
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "original"

    def test_only_the_last_extension_is_split(self, tmp_path):
        """``archive.tar.gz`` → ``archive.tar-1.gz``。这条是**规格**不是巧合：

        双扩展名白名单（.tar.bz2 / .tar.xz / .tar.zst / .min.js / .d.ts …）永远补不完，
        漏一个就冒出第二种归名风格。代价是 ``archive.tar-1.gz`` 不好看，但双击照常解压。
        """
        (tmp_path / "archive.tar.gz").write_bytes(b"x")
        assert unique_destination(str(tmp_path), "archive.tar.gz") == (
            "archive.tar-1.gz", True,
        )

    def test_dotfiles_and_extensionless_names(self, tmp_path):
        (tmp_path / ".env").write_bytes(b"x")
        assert unique_destination(str(tmp_path), ".env") == (".env-1", True)
        (tmp_path / "README").write_bytes(b"x")
        assert unique_destination(str(tmp_path), "README") == ("README-1", True)

    def test_skips_existing_directories(self, tmp_path):
        """目录同名也要让开：``os.replace`` 到目录上会抛 IsADirectoryError。"""
        (tmp_path / "a").mkdir()
        assert unique_destination(str(tmp_path), "a") == ("a-1", True)

    def test_skips_dangling_symlinks(self, tmp_path):
        """``exists`` 对悬空软链返回 False，用它就会把软链本身覆盖掉。"""
        os.symlink(str(tmp_path / "gone"), str(tmp_path / "a.txt"))
        assert unique_destination(str(tmp_path), "a.txt") == ("a-1.txt", True)

    def test_refuses_to_overflow_name_max(self, tmp_path):
        """原名贴着 255 字节时放不下 ``-1``。报错，而不是悄悄截断 stem。"""
        name = "中" * 85  # 正好 255 字节
        (tmp_path / name).write_bytes(b"x")
        with pytest.raises(ValueError):
            unique_destination(str(tmp_path), name)


class TestReceiveUpload:
    """``receive_upload`` 是协程，但仓库里没装 pytest-asyncio（也没有任何 async 测试），
    所以这里用 ``asyncio.run`` 自己驱动，而不是为六条用例引一个新依赖。"""

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    def test_writes_exact_bytes(self, tmp_path):
        result = self._run(receive_upload(str(tmp_path), "a.txt", _chunks(b"hello world")))
        assert result == {"name": "a.txt", "path": "a.txt", "size": 11, "renamed": False}
        assert (tmp_path / "a.txt").read_bytes() == b"hello world"

    def test_empty_payload_is_not_an_error(self, tmp_path):
        result = self._run(receive_upload(str(tmp_path), "empty.txt", _chunks(b"")))
        assert result["size"] == 0
        assert (tmp_path / "empty.txt").read_bytes() == b""

    def test_second_upload_renames_and_preserves_the_first(self, tmp_path):
        self._run(receive_upload(str(tmp_path), "a.txt", _chunks(b"first")))
        result = self._run(receive_upload(str(tmp_path), "a.txt", _chunks(b"second")))
        assert result["name"] == "a-1.txt" and result["renamed"] is True
        assert (tmp_path / "a.txt").read_bytes() == b"first"
        assert (tmp_path / "a-1.txt").read_bytes() == b"second"

    def test_size_limit_leaves_no_partial_file(self, tmp_path):
        with pytest.raises(UploadTooLarge):
            self._run(receive_upload(
                str(tmp_path), "big.bin", _chunks(b"x" * 64), max_bytes=16,
            ))
        assert os.listdir(tmp_path) == []

    def test_interrupted_stream_leaves_no_partial_file(self, tmp_path):
        async def exploding():
            yield b"partial"
            raise ConnectionResetError("client went away")

        with pytest.raises(ConnectionResetError):
            self._run(receive_upload(str(tmp_path), "a.txt", exploding()))
        assert os.listdir(tmp_path) == []

    def test_exactly_at_the_limit_passes(self, tmp_path):
        result = self._run(receive_upload(
            str(tmp_path), "a.bin", _chunks(b"x" * 16), max_bytes=16,
        ))
        assert result["size"] == 16

    def test_concurrent_same_name_uploads_all_survive(self, tmp_path):
        """并发是前端的行为（3 路），不是假设。

        「解析不冲突的名字」与「os.replace」如果不在同一把锁里，两个同名的并发请求
        会双双算出 ``a-1.txt``，后 replace 的那个覆盖先落的那个 —— 用户丢掉一份文件，
        而且是静默的。
        """
        async def upload_all():
            return await asyncio.gather(*[
                receive_upload(str(tmp_path), "a.txt", _chunks(bytes([65 + index]) * 8))
                for index in range(8)
            ])

        results = asyncio.run(upload_all())
        names = [item["name"] for item in results]
        assert len(set(names)) == 8                       # 8 个请求 8 个不同的名字
        assert sorted(os.listdir(tmp_path)) == sorted(names)   # 盘上正好是这 8 个
        for index, item in enumerate(results):            # 每份内容都对得上自己的请求
            assert (tmp_path / item["name"]).read_bytes() == bytes([65 + index]) * 8
