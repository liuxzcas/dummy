"""garage —— dummy 的临时工作区。

============ 为什么需要它(2026-11-02 用户提出) ============
用户原话:"所有为了干活而生成的临时程序、临时脚本、临时文件
(除非是用户指定要求的)都放在一个固定的路径下运行或修改,
这样方便管理和清理。"

目标两条:
  ① 临时产物集中放 —— 好找、好清
  ② 有了这个约定,危险操作黑名单可以放宽(写 garage 内不算危险)

============ 为什么叫 garage 而不是 base_ws ============
base_ws/ 是用户**私人工作区**(机电2502 那些 docx 和备份),
明确要求"绝不进 repo"。临时产物混进去会很危险 ——
清理时可能误删私人文件。所以另开一个 garage/,两者职责分开:

    base_ws/  用户的, dummy 不碰, 永不清理
    garage/   dummy 的, 可随时清, 内容不入库

============ 相对路径的落点(用户 2026-11-02 定) ============
    write_file("foo.py")        → garage/foo.py      (相对路径进 garage)
    write_file("D:/x/foo.py")   → D:/x/foo.py        (绝对路径不动)
    terminal("python foo.py")   → 子进程 cwd = garage

这样模型干活产生的文件默认都归到一处,而"交付物"用绝对路径写。

============ 清理(用户选 b:手动) ============
不自动清 —— 每轮结束就删可能删掉模型还要用的中间文件。
提供 /workspace clean 命令手动清。
"""
import os

# garage 目录(本文件所在目录下的 garage/)
GARAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "garage")


def ensure() -> str:
    """确保 garage 目录存在,返回其绝对路径。"""
    os.makedirs(GARAGE_DIR, exist_ok=True)
    return GARAGE_DIR


def resolve(path: str) -> str:
    """把工具收到的路径解析成绝对路径:相对路径一律落到 garage 下。

    ============ 为什么不用 os.getcwd()(2026-11-02) ============
    原来是 `os.path.abspath(path)` —— 相对路径跟着进程 cwd 走。
    但 dummy 可能从任意目录启动(启动器、沙箱、脚本),
    cwd 不一定等于项目根,模型写的 "foo.py" 会落到意想不到的地方。
    固定到 garage 后行为可预测:相对路径 = garage 里的文件。
    """
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.abspath(path)
    return os.path.join(ensure(), path)


def is_inside_garage(path: str) -> bool:
    """判断一个(已解析的)绝对路径是否在 garage 里。"""
    root = os.path.abspath(GARAGE_DIR)
    target = os.path.abspath(path)
    return target == root or target.startswith(root + os.sep)


def clean() -> tuple[int, int]:
    """清空 garage(保留目录本身和 .gitkeep)。返回 (删除的文件数, 释放字节数)。"""
    import shutil

    ensure()
    files = 0
    size = 0
    for name in os.listdir(GARAGE_DIR):
        if name == ".gitkeep":
            continue
        full = os.path.join(GARAGE_DIR, name)
        try:
            if os.path.isdir(full):
                for dirpath, _dirnames, filenames in os.walk(full):
                    for f in filenames:
                        try:
                            size += os.path.getsize(os.path.join(dirpath, f))
                            files += 1
                        except OSError:
                            pass
                shutil.rmtree(full)
            else:
                size += os.path.getsize(full)
                files += 1
                os.remove(full)
        except OSError:
            pass
    return files, size


def listing() -> list[tuple[str, int]]:
    """列出 garage 里的内容,返回 [(相对名, 字节数), ...](目录递归统计)。"""
    ensure()
    out = []
    for name in sorted(os.listdir(GARAGE_DIR)):
        if name == ".gitkeep":
            continue
        full = os.path.join(GARAGE_DIR, name)
        if os.path.isdir(full):
            total = 0
            for dirpath, _d, filenames in os.walk(full):
                for f in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
            out.append((name + "/", total))
        else:
            try:
                out.append((name, os.path.getsize(full)))
            except OSError:
                out.append((name, 0))
    return out
