"""输入源抽象 —— 打断信号的统一入口（2026-11-11 输入架构改造 第 2 步）。

============ 要解决什么 ============
用户要求："agent 处理 tool calling 时依旧等待用户输入，
如果按下回车才打断并注入用户输入的内容。"

完整方案是让 prompt_toolkit 的 Application 常驻主线程、agent 跑后台
线程，用户输入经 **queue** 送进 agent（Hermes 的做法）。那是第 3-5 步。

但 core 不该关心"输入从哪来"。所以先把接口抽出来：

    InputSource
      ├── ListenerSource   ← 现在：msvcrt 监听器（第 2 步用这个）
      └── QueueSource      ← 第 3-4 步：prompt_toolkit 提交的 queue

core 只调两个方法：
    take()       取一行（没有返回 None）—— 检查点用
    poll()       有中断待处理吗？（**消费**）—— 长命令执行期间用

============ 为什么现在就要做这层 ============
不然第 3-5 步要同时改 core 的检查点、terminal 的探针、main 的主循环，
三处一起动，出问题无法归因。抽出来之后，第 3-4 步只换一个实现类。

============ 两个队列的职责（照抄 Hermes）============
Hermes cli.py:6083
    self._interrupt_queue.put(text) if self.busy_input_mode == "interrupt"
    else self._pending_input.put(text)

| 队列 | 什么时候进 | 消费方 |
|---|---|---|
| interrupt | agent **忙**时用户回车 | agent 检查点 → 打断 + 注入 |
| pending   | agent **空闲**时用户回车 | 主循环 → 下一轮任务 |
"""
import queue


class InputSource:
    """输入源基类 —— 只定义契约。"""

    def take(self) -> str | None:
        """取一行（没有则 None）。**会消费**。"""
        raise NotImplementedError

    def poll(self) -> bool:
        """是否有中断待处理。**会消费** —— 拿到就吃掉，避免重复触发。

        (第一版探针用的是"只读不消费"，结果 /p 留在缓冲区里，
         下一轮每轮都自动触发。用户实测报障过，别再犯。)
        """
        raise NotImplementedError

    def drain(self) -> None:
        """清空残留（本轮结束时调用，防止幽灵输入）。"""
        raise NotImplementedError


class ListenerSource(InputSource):
    """现在的实现：msvcrt 后台监听器。

    语义保持与原 _drain_interrupt 一致：只有 /p 前缀算打断。
    """

    def __init__(self, listener):
        self._listener = listener

    def take(self) -> str | None:
        return self._listener.take_line()

    def poll(self) -> bool:
        # consume_interrupt：只吃掉 /p 那一行，其他行原样留着
        return self._listener.consume_interrupt()

    def drain(self) -> None:
        while self._listener.take_line() is not None:
            pass


class QueueSource(InputSource):
    """第 3-4 步用：prompt_toolkit 应用把用户回车的内容 put 进来。

    ============ 与 ListenerSource 的语义差异 ============
    · 不再要求 /p 前缀 —— **任何非空行**都算打断
      （用户："按下回车，才打断并注入用户输入的内容"）
    · 输入内容要**保留**（用于注入），不能只返回 bool
    · /p 前缀仍然接受（兼容旧习惯），但注入时要剥掉

    ============ 为什么用 0.1 秒超时的 get ============
    照抄 Hermes cli.py:12430。blocking get 让长命令执行期间
    检查点能"边等边看"；0.1 秒保证打断延迟可接受。
    """

    def __init__(self, q: queue.Queue):
        self._q = q

    def take(self) -> str | None:
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def poll(self) -> bool:
        """★ 注意：这里只探测不消费（与 ListenerSource 不同）。

        原因：队列里的内容**有用**（要注入给模型），
        不能像 /p 那样取走就丢。
        """
        try:
            self._q.queue[0]        # deque 首元素，只读
            return True
        except (IndexError, AttributeError):
            return not self._q.empty()

    def drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return


def strip_prefix(line: str) -> str:
    """去掉 /p 前缀（如果用户还习惯敲的话）。"""
    s = (line or "").strip()
    if s.startswith("/p"):
        return s[2:].strip()
    return s
