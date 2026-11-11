"""Terminal progress for the existing simulation runner, with a full log file."""
from __future__ import annotations

import argparse
from collections import deque
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from typing import TextIO


STAGES = (
    ('Authenticate sudo', '检查运行权限'),
    ('Clean previous runtime and configured network', '准备运行目录与网络'),
    ('Generate PLC programs', '生成 PLC 程序'),
    ('Generate namespace setup', '生成网络初始化脚本'),
    ('Generate ns-3 topology', '生成 ns-3 拓扑'),
    ('Compile PLC programs', '编译 PLC 程序'),
    ('Compile ns-3 topology', '编译 ns-3 拓扑'),
    ('Create network namespaces', '创建网络环境'),
    ('Launch OpenPLC', '启动 OpenPLC'),
    ('Start ns-3 network', '启动 ns-3 网络'),
)
CYCLE_DONE = re.compile(r'^\[CYCLE (\d+)\] timing ')


def safe_text(text: str) -> str:
    """Keep paths and child output from introducing terminal control sequences."""
    return ''.join(c if c.isprintable() else ' ' for c in text)


def bar(done: int, total: int, width: int = 24) -> str:
    fraction = min(1.0, max(0.0, done / total)) if total else 1.0
    filled = int(fraction * width)
    return '[' + '=' * filled + ' ' * (width - filled) + f'] {fraction:6.1%} {done}/{total}'


class ProgressDisplay:
    def __init__(self, label: str, cycles: int, stream: TextIO | None = None) -> None:
        self.label = safe_text(label)
        self.cycles = cycles
        self.stream = sys.stdout if stream is None else stream
        self.tty = self.stream.isatty() and os.environ.get('TERM') != 'dumb'
        self.initialized = 0
        self.completed = 0
        self.exported = 0
        self.status = '准备开始'
        self.phase = 'initialization'
        self.started = time.monotonic()
        self.drawn = False
        self.last_plain_key = None

    def consume(self, line: str) -> bool:
        if line.startswith('[RUN-ALL] '):
            stage = line.strip().removeprefix('[RUN-ALL] ')
            for index, (name, label) in enumerate(STAGES):
                if stage == name:
                    self.initialized = index
                    self.status = label
                    return True
            if stage == 'Run closed loop':
                self.initialized = len(STAGES)
                self.phase = 'simulation'
                self.status = '初始化水力状态与 PLC 数据'
            elif stage == 'Export results':
                self.phase = 'export'
                self.status = '导出结果'
            return True
        match = CYCLE_DONE.match(line)
        if match:
            self.completed = min(self.cycles, max(self.completed, int(match[1])))
            self.status = '运行仿真'
            return True
        if line.startswith('[INIT]') and '.ready released' in line:
            self.status = '运行仿真'
            return True
        if line.startswith('[DONE] Results:'):
            self.exported = 1
            self.status = '清理运行进程'
            return True
        return False

    def render(self, *, final: bool = False) -> None:
        elapsed = time.monotonic() - self.started
        # Fixed-width bars keep the terminal readable at ordinary window sizes.
        lines = [
            'Hydro-CPS-Sim V' + (Path(__file__).resolve().parents[2] / 'VERSION').read_text().strip(),
            f'配置: {self.label}',
            f'初始化 {bar(self.initialized, len(STAGES))}',
            f'仿真   {bar(self.completed, self.cycles)}',
            f'导出   {bar(self.exported, 1)}',
            f'状态: {self.status} | 已用时 {elapsed:.1f}s',
        ]
        if self.tty:
            if self.drawn:
                self.stream.write('\x1b[6A')
            terminal_columns = shutil.get_terminal_size((80, 24)).columns
            columns = max(20, (terminal_columns or 80) - 1)
            for line in lines:
                # Each CJK character occupies at most two terminal columns.
                clipped = ''
                width = 0
                for char in line:
                    width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
                    if width > columns:
                        break
                    clipped += char
                self.stream.write('\r\x1b[2K' + clipped + '\n')
            self.drawn = True
        else:
            key = (self.initialized, self.phase, self.completed * 10 // max(1, self.cycles), self.exported)
            if not final and key == self.last_plain_key:
                return
            self.last_plain_key = key
            self.stream.write(' | '.join(lines[2:]) + '\n')
        self.stream.flush()


def run(command: list[str], log_dir: Path, label: str, iterations: int) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    display = ProgressDisplay(label, max(0, iterations - 1))
    tail: deque[str] = deque(maxlen=25)
    messages: queue.Queue[str | None] = queue.Queue(maxsize=1024)
    interrupted = 0
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='run_all-', suffix='.log',
                                     dir=log_dir, delete=False) as log:
        log_path = Path(log.name).resolve()
        print(f'详细日志: {log_path}', flush=True)
        env = dict(os.environ, HYDRO_PROGRESS_CHILD='1', PYTHONUNBUFFERED='1')
        try:
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding='utf-8', errors='replace', env=env,
                                     preexec_fn=os.setpgrp)
        except OSError as exc:
            log.write(str(exc) + '\n')
            print(f'[ERROR] 无法启动运行程序: {exc}', file=sys.stderr)
            return 1

        def forward(signum: int, _frame: object) -> None:
            nonlocal interrupted
            interrupted = signum
            display.status = '正在停止并清理'
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass

        previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}

        def read_output() -> None:
            assert child.stdout is not None
            try:
                for line in child.stdout:
                    messages.put(line)
            finally:
                child.stdout.close()
                messages.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        display.render()
        try:
            while True:
                try:
                    line = messages.get(timeout=0.2)
                except queue.Empty:
                    if display.tty:
                        display.render()
                    continue
                if line is None:
                    break
                log.write(line)
                log.flush()
                tail.append(line.rstrip())
                if display.consume(line):
                    display.render()
            rc = child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        rc = 128 + interrupted if interrupted else (128 - rc if rc < 0 else rc)
        display.status = '已完成' if rc == 0 else ('已中止' if interrupted else f'运行失败（退出码 {rc}）')
        display.render(final=True)
        if rc:
            print('\n运行日志末尾:', file=sys.stderr)
            for line in tail:
                print(safe_text(line), file=sys.stderr)
        print(f'详细日志: {log_path}', flush=True)
        return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log-dir', required=True, type=Path)
    parser.add_argument('--label', required=True)
    parser.add_argument('--iterations', required=True, type=int)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a runner command is required after --')
    return run(command, args.log_dir, args.label, args.iterations)


if __name__ == '__main__':
    raise SystemExit(main())
