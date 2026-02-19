#!/usr/bin/env python3
"""
SP-03: FIFO 多写者并发行为验证

验证目标:
  1. 多进程并发写同一 FIFO，行是否会交叉（原子性）
  2. O_NONBLOCK 模式下，无写者时 open/read 的行为
  3. 写者全部退出后，读者感知 EOF 的方式

运行: python3 run.py
结果写入: sp03_results.txt
"""

import os
import sys
import time
import select
import multiprocessing
import tempfile
import stat
import errno
import fcntl

FIFO_PATH = "/tmp/ccmux_sp03_test.fifo"
RESULTS_FILE = os.path.join(os.path.dirname(__file__), "sp03_results.txt")
WRITERS = 5
LINES_PER_WRITER = 50
LINE_SIZE_SHORT = 64          # 远小于 PIPE_BUF (4096)
LINE_SIZE_LONG  = 8192        # 超过 PIPE_BUF

results = []

def log(msg):
    print(msg)
    results.append(msg)


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def make_fifo():
    if os.path.exists(FIFO_PATH):
        os.unlink(FIFO_PATH)
    os.mkfifo(FIFO_PATH)


def writer_proc(writer_id: int, line_size: int, n_lines: int):
    """写者进程：写入 n_lines 行，每行内容可识别唯一来源"""
    with open(FIFO_PATH, "w") as f:
        for i in range(n_lines):
            # 格式: W<id>:L<lineno>:<padding>\n
            prefix = f"W{writer_id}:L{i:04d}:"
            padding = "x" * (line_size - len(prefix) - 1)
            line = prefix + padding + "\n"
            f.write(line)
            f.flush()


def reader_all(fifo_path, timeout=10.0) -> list[str]:
    """读取 FIFO 所有内容直到 EOF 或超时（用 select 避免 readline 永久阻塞）"""
    lines = []
    buf = ""
    deadline = time.time() + timeout
    fd = os.open(fifo_path, os.O_RDONLY)
    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:  # EOF：所有写者已关闭
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                lines.append(line + "\n")
    finally:
        os.close(fd)
    return lines


# ─────────────────────────────────────────────
# 测试 1：短消息并发写，检查原子性
# ─────────────────────────────────────────────

def test_atomic_short():
    log("\n=== TEST 1: 短消息并发写原子性 (line_size=%d, PIPE_BUF=4096) ===" % LINE_SIZE_SHORT)
    make_fifo()

    procs = [
        multiprocessing.Process(target=writer_proc, args=(i, LINE_SIZE_SHORT, LINES_PER_WRITER))
        for i in range(WRITERS)
    ]

    # 先启动读者（后台线程），再启动写者
    # 增大延迟确保读者 fd 已就绪，避免部分写者在读者打开之前就写完并关闭
    pool = multiprocessing.pool.ThreadPool(1)
    reader_future = pool.apply_async(reader_all, (FIFO_PATH,))

    time.sleep(0.3)
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    lines = reader_future.get(timeout=15)
    pool.close()

    total_expected = WRITERS * LINES_PER_WRITER
    log(f"  期望行数: {total_expected}")
    log(f"  实际行数: {len(lines)}")

    corrupted = 0
    for line in lines:
        line = line.rstrip("\n")
        if not (line.startswith("W") and ":L" in line):
            corrupted += 1
            log(f"  !! 损坏行: {repr(line[:80])}")

    log(f"  损坏行数: {corrupted}")
    log(f"  结论: {'✅ 原子写入，无数据交叉' if corrupted == 0 else '❌ 存在数据交叉'}")
    os.unlink(FIFO_PATH)


# ─────────────────────────────────────────────
# 测试 2：长消息并发写（超过 PIPE_BUF）
# ─────────────────────────────────────────────

def test_atomic_long():
    log("\n=== TEST 2: 长消息并发写（line_size=%d > PIPE_BUF） ===" % LINE_SIZE_LONG)
    make_fifo()

    # 减少并发和行数，长消息更容易复现问题
    n_writers = 3
    n_lines = 10
    procs = [
        multiprocessing.Process(target=writer_proc, args=(i, LINE_SIZE_LONG, n_lines))
        for i in range(n_writers)
    ]

    pool = multiprocessing.pool.ThreadPool(1)
    reader_future = pool.apply_async(reader_all, (FIFO_PATH, 8.0))

    time.sleep(0.3)
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    lines = reader_future.get(timeout=15)
    pool.close()

    total_expected = n_writers * n_lines
    log(f"  期望行数: {total_expected}")
    log(f"  实际行数: {len(lines)}")

    corrupted = 0
    for line in lines:
        stripped = line.rstrip("\n")
        if not (stripped.startswith("W") and ":L" in stripped):
            corrupted += 1
            log(f"  !! 损坏行 (前80字节): {repr(stripped[:80])}")

    log(f"  损坏行数: {corrupted}")
    if len(lines) < total_expected:
        log(f"  ⚠️  行数不足（{len(lines)}/{total_expected}）：非原子写导致部分行无换行 → readline 阻塞 → 超时截断")
        log(f"  结论: ⚠️  长消息（>PIPE_BUF）非原子写，存在数据交叉且有死锁风险，需要应用层加锁/限制消息大小")
    elif corrupted > 0:
        log(f"  结论: ⚠️  长消息存在交叉，需要应用层加锁")
    else:
        log(f"  结论: ✅ 未发现交叉（但不保证，建议保持消息 < PIPE_BUF=4096B）")
    os.unlink(FIFO_PATH)


# ─────────────────────────────────────────────
# 测试 3：O_NONBLOCK open，无写者时的行为
# ─────────────────────────────────────────────

def test_nonblock_open():
    log("\n=== TEST 3: O_NONBLOCK open 无写者时的行为 ===")
    make_fifo()

    try:
        fd = os.open(FIFO_PATH, os.O_RDONLY | os.O_NONBLOCK)
        log("  ✅ O_NONBLOCK open 成功（无需等待写者）")

        # 尝试读取
        try:
            data = os.read(fd, 1024)
            log(f"  read 返回: {repr(data)} (空则为 EOF)")
        except BlockingIOError:
            log("  read 抛出 BlockingIOError (EAGAIN) — 无数据可读，符合预期")
        except OSError as e:
            log(f"  read 抛出 OSError: {e}")
        finally:
            os.close(fd)

    except OSError as e:
        if e.errno == errno.ENXIO:
            log("  ❌ open 抛出 ENXIO — O_NONBLOCK 下无写者时无法打开读端")
        else:
            log(f"  ❌ open 抛出未知 OSError: {e}")

    os.unlink(FIFO_PATH)


# ─────────────────────────────────────────────
# 测试 4：写者全部退出后，读者感知 EOF
# ─────────────────────────────────────────────

def test_eof_detection():
    log("\n=== TEST 4: 写者退出后读者如何感知 EOF ===")
    make_fifo()

    def write_and_close():
        with open(FIFO_PATH, "w") as f:
            f.write("hello\n")
            f.flush()
        # 写者退出，关闭写端

    # 先开读者（blocking open，等写者）
    def read_until_eof():
        lines = []
        with open(FIFO_PATH, "r") as f:
            for line in f:
                lines.append(line)
        return lines

    pool = multiprocessing.pool.ThreadPool(1)
    reader_future = pool.apply_async(read_until_eof)

    time.sleep(0.1)
    w = multiprocessing.Process(target=write_and_close)
    w.start()
    w.join()

    lines = reader_future.get(timeout=5)
    pool.close()

    log(f"  读到内容: {lines}")
    expected = ["hello\n"]
    log(f"  结论: {'✅ 写者退出后读者正常收到 EOF' if lines == expected else '⚠️  异常'}")
    os.unlink(FIFO_PATH)


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import multiprocessing.pool  # noqa

    log("SP-03: FIFO 并发行为验证")
    log(f"Python {sys.version}")
    log(f"平台: {sys.platform}")

    test_nonblock_open()
    test_eof_detection()
    test_atomic_short()
    test_atomic_long()

    log("\n=== 完成，需要固化到 spec.md 的结论 ===")
    log("  [ ] 短消息（<4096B）写入是否原子")
    log("  [ ] O_NONBLOCK open 无写者时的行为（ENXIO / 成功）")
    log("  [ ] daemon 读 FIFO 的正确模式")

    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(results) + "\n")
    log(f"\n结果已写入: {RESULTS_FILE}")
