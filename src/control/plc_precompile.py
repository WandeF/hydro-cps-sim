"""
模块说明：
    本脚本用于批量预编译项目中的 ST 文件，并将 OpenPLC 生成的运行时二进制导出为
    独立的 PLC 可执行文件，供后续在不同命名空间中分别启动。

主要功能：
    1. 扫描 examples/c_town/output/st 目录下的所有 .st 文件；
    2. 调用 OpenPLC 编译脚本逐个编译 ST 程序；
    3. 将生成的 openplc 二进制复制并重命名到 output/plcs 目录；
    4. 为导出的 PLC 可执行文件补充执行权限。

输入参数：
    --openplc-root   OpenPLC_v3 根目录。

输出结果：
    在 examples/c_town/output/plcs 下生成与 ST 文件同名的 PLC 可执行文件。

适用场景：
    用于多 PLC 场景下的离线预编译，避免每次启动时重复通过 WebServer 动态编译，
    便于后续结合 netns、ns-3 和批量启动脚本进行统一调度。
"""

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def resolve_output_dir(config_path: Path, cfg: dict[str, Any]) -> Path:
    raw = cfg.get("output_path")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("config.yaml missing valid top-level output_path")
    p = Path(raw).expanduser()
    if p.is_absolute():
        if p.exists():
            return p.resolve()
        local_output = (config_path.parent / "output").resolve()
        if local_output.exists():
            return local_output
        return p.resolve()
    return (config_path.parent / p).resolve()


def resolve_openplc_root(config_path: Path, cfg: dict[str, Any], cli_value: str | None) -> Path:
    raw = cli_value or cfg.get("openplc_path") or "../OpenPLC_v3"
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (config_path.parent / p).resolve()

def run_cmd(cmd: list[str], cwd: Path) -> None:
    print(f"[RUN] cwd={cwd}")
    print(f"[CMD] {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(cwd))
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {' '.join(cmd)}")


def ensure_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def copy_binary_atomic(src: Path, dst: Path) -> None:
    """Export an OpenPLC runtime binary without writing into dst in-place.

    If a previous run is still executing output/plcs/plcN, opening that file for
    writing raises ETXTBSY ("Text file busy"). Copying to a temporary file and
    then replacing the path atomically avoids modifying the executing inode; the
    old process keeps using the old inode, while later launches see the new file.
    The launcher still performs stale-process cleanup before starting PLCs.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    try:
        if tmp.exists():
            tmp.unlink()
        shutil.copy2(src, tmp)
        ensure_executable(tmp)
        os.replace(tmp, dst)
        ensure_executable(dst)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch compile ST files with OpenPLC and export renamed PLC binaries."
    )
    parser.add_argument(
        "--config",
        default="examples/c_town/config.yaml",
        help="Path to Hydro-CPS config.yaml; output_path/openplc_path are read from it.",
    )
    parser.add_argument(
        "--openplc-root",
        default=None,
        help="Override OpenPLC_v3 root directory, e.g. /home/lzh/MASTER/CODE/OpenPLC_v3",
    )
    args = parser.parse_args()

    script_path = Path(__file__).resolve()
    project_root = script_path.parents[2]  # repository root

    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = (project_root / config_path).resolve()
    else:
        config_path = config_path.resolve()
    if not config_path.exists():
        print(f"[Error] Config file not found: {config_path}")
        return 1

    cfg = load_yaml(config_path)
    output_dir = resolve_output_dir(config_path, cfg)
    openplc_root = resolve_openplc_root(config_path, cfg, args.openplc_root)
    if not openplc_root.exists():
        print(f"[Error] OpenPLC root not found: {openplc_root}")
        return 1

    webserver_dir = openplc_root / "webserver"
    st_files_dir = webserver_dir / "st_files"
    db_path = webserver_dir / "openplc.db"
    active_program_path = webserver_dir / "active_program"
    compile_script = webserver_dir / "scripts" / "compile_program.sh"
    built_binary = webserver_dir / "core" / "openplc"

    st_input_dir = output_dir / "st"
    plc_output_dir = output_dir / "plcs"

    if not webserver_dir.exists():
        print(f"[Error] OpenPLC webserver dir not found: {webserver_dir}")
        return 1
    if not compile_script.exists():
        print(f"[Error] OpenPLC compile script not found: {compile_script}")
        return 1
    if not st_input_dir.exists():
        print(f"[Error] ST input dir not found: {st_input_dir}")
        return 1

    st_files_dir.mkdir(parents=True, exist_ok=True)
    plc_output_dir.mkdir(parents=True, exist_ok=True)

    st_list = sorted(st_input_dir.glob("*.st"))
    if not st_list:
        print(f"[Error] No .st files found in: {st_input_dir}")
        return 1

    print("[INFO] Config            :", config_path)
    print("[INFO] OpenPLC root      :", openplc_root)
    print("[INFO] ST input dir      :", st_input_dir)
    print("[INFO] PLC output dir    :", plc_output_dir)
    print("[INFO] openplc.db path   :", db_path)
    print("[INFO] active_program    :", active_program_path)
    print("[INFO] Found ST files:")
    for st in st_list:
        print(f"  - {st.name}")

    for src_st_file in st_list:
        plc_name = src_st_file.stem
        dst_st_file = st_files_dir / src_st_file.name
        dst_plc_file = plc_output_dir / plc_name

        print(f"\n[INFO] Processing {src_st_file.name}")

        # 1. 复制 ST 到 OpenPLC 的 st_files 目录
        print(f"[INFO] Copying {src_st_file} -> {dst_st_file}")
        shutil.copy2(src_st_file, dst_st_file)

        # 2. 调用 OpenPLC 编译
        run_cmd(["bash", str(compile_script), src_st_file.name], cwd=webserver_dir)

        # 3. 检查编译产物
        if not built_binary.exists():
            print(f"[Error] Built binary not found: {built_binary}")
            return 1

        # 4. 复制并重命名到输出目录。不要原地覆盖 dst_plc_file：
        # 如果上一轮 run_all 后 PLC 还在运行，Linux 会对正在执行的
        # 二进制返回 ETXTBSY(Text file busy)。这里使用临时文件 + 原子替换。
        print(f"[INFO] Exporting binary {built_binary} -> {dst_plc_file}")
        copy_binary_atomic(built_binary, dst_plc_file)

        print(f"[OK] Exported: {dst_plc_file}")

    print("\n[DONE] All ST files compiled successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[FATAL] {e}")
        raise SystemExit(1)