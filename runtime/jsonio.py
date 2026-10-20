"""JSON 落盘的小工具：一份原子写的实现，给所有「配置文件不能写坏」的地方共用。

为什么单独一个模块而不是放在调用方旁边：原子写这条要求来自**文件的性质**（它是重启后
恢复状态的唯一依据），不来自任何某个 manager。各写一遍的话，新加一处配置存储时很容易
只写 ``json.dump`` 了事 —— 那在正常运行下看不出区别，只有掉电或进程被杀的那一刻才暴露，
而那一刻正是这份文件唯一被需要的时候。

放这里而不是 ``runtime/persistence.py``：那个模块是 Graph 快照的 ``Persistence`` 类，
带定时保存与双文件容错，是个有状态的东西；这里只有一个纯函数。
"""
from __future__ import annotations

import json
import os
from typing import Any


def atomic_write_json(path: str, payload: Any, *, indent: int | None = 2) -> None:
    """把 payload 以 JSON 原子写入 path。

    写 tmp + flush + fsync + os.replace：半截文件会让整个文件读不出来，所以必须
    落盘后再替换。``ensure_ascii=False`` —— 中文可读性要保住。

    ``indent`` 默认两空格，适合给人看、也会进 git 的配置文件；机器读的事件队列
    文件传 ``None`` 保留紧凑格式。默认值必须稳定：这些文件的字节形态已被测试钉住。

    调用方自行负责并发（同一路径不会有两个写者）。
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=indent)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp_path, path)
