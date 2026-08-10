#!/usr/bin/env python3
"""vmkit logfilter 测试。运行: python3 vmkit/tests/test_logfilter.py"""
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import vmkit  # noqa: E402

VMKIT = os.path.join(os.path.dirname(__file__), "..", "vmkit.py")

SAMPLE = (
    "[\x1b[36min-vm\x1b[0m 2026-08-09T18:02:42Z] 以 zfsbuild 身份启动 zfs-tests.sh（看门狗 900s）...\n"
    "  CC [M]  drivers/block/zram/zram_drv.o\n"
    "[in-vm 2026-08-09T18:02:42Z] 生成 compat.run（组: cli_root,...）\n"
    "[2026-08-09T18:21:35] Test: .../zfs_create/cleanup [00:00] [PASS]\n"
    "\n"
)

PROGRESS_SAMPLE = (
    "  % Total    % Received % Xferd  Average Speed  Time    Time    Time   Current\n"
    "                                 Dload  Upload  Total   Spent   Left   Speed\n"
    "\r  0      0   0      0   0      0      0      0      0      0      0      0"
    "\r100 237.3k 100 237.3k   0      0 104.0k      0   00:02   00:02          54587\n"
    "[in-vm 2026-08-09T19:47:44Z] 下载 kernel-core ...\n"
)


def run(level):
    p = subprocess.run(
        [sys.executable, VMKIT, "logfilter", level],
        input=SAMPLE, capture_output=True, text=True,
    )
    return p.returncode, p.stdout


def run_progress(level):
    p = subprocess.run(
        [sys.executable, VMKIT, "logfilter", level],
        input=PROGRESS_SAMPLE, capture_output=True, text=True,
    )
    return p.returncode, p.stdout


def test_colorize_results():
    assert vmkit.colorize_results(b"[PASS]", False) == b"[PASS]"
    assert vmkit.colorize_results(b"[FAIL]", True) == b"\x1b[31m[FAIL]\x1b[0m"
    assert vmkit.colorize_results(b"[KILLED]", True) == b"\x1b[31m[KILLED]\x1b[0m"
    assert vmkit.colorize_results(b"[SKIP]", True) == b"\x1b[33m[SKIP]\x1b[0m"
    assert vmkit.colorize_results(b"Test: ... [00:00] [PASS]", True) == \
        b"Test: ... [00:00] \x1b[32m[PASS]\x1b[0m"


def test_logfilter_function_bytesio():
    src = io.BytesIO(SAMPLE.encode())
    out = io.BytesIO()
    assert vmkit.logfilter("verbose", src, out) == 0
    text = out.getvalue().decode()
    assert "CC [M]" in text and "Test:" in text
    assert text.count("[in-vm ") >= 2


def test_verbose_prefixes_raw_vm_lines():
    rc, out = run("verbose")
    assert rc == 0
    lines = out.splitlines()
    # 阶段标记行保留原样（含颜色），不重复加前缀
    assert lines[0] == "[\x1b[36min-vm\x1b[0m 2026-08-09T18:02:42Z] 以 zfsbuild 身份启动 zfs-tests.sh（看门狗 900s）..."
    assert lines[2] == "[in-vm 2026-08-09T18:02:42Z] 生成 compat.run（组: cli_root,...）"
    # 非阶段标记行统一补 [in-vm <UTC时间戳>] 前缀
    assert lines[1].startswith("[in-vm ") and "CC [M]" in lines[1]
    assert lines[3].startswith("[in-vm ") and "Test:" in lines[3]
    # 空行保持原样
    assert out.endswith("\n\n")


def test_verbose_merges_cr_progress_updates():
    rc, out = run_progress("verbose")
    assert rc == 0
    lines = out.splitlines()
    assert len(lines) == 4
    # 两个表头行正常加前缀
    assert lines[0].startswith("[in-vm ") and "% Total" in lines[0]
    assert lines[1].startswith("[in-vm ") and lines[1].endswith("Speed")
    # 进度条多次 \r 重绘只保留最终一次，且只加一个前缀
    assert lines[2].startswith("[in-vm ") and lines[2].endswith("54587")
    assert out.count("104.0k") == 1
    # 阶段标记行原样保留
    assert lines[3] == "[in-vm 2026-08-09T19:47:44Z] 下载 kernel-core ..."


def test_normal_drops_progress_but_keeps_marker():
    rc, out = run_progress("normal")
    assert rc == 0
    assert out.splitlines() == ["[in-vm 2026-08-09T19:47:44Z] 下载 kernel-core ..."]


def test_normal_keeps_only_invm_lines():
    rc, out = run("normal")
    assert rc == 0
    lines = out.splitlines()
    assert len(lines) == 2                     # 彩色 + 无色各一条 [in-vm]
    # 注意: 彩色行里 [ 与 in-vm 之间有 ANSI 码，只断言 "in-vm" 在行内
    assert all("in-vm" in l for l in lines)
    assert all("CC [M]" not in l and "Test:" not in l for l in lines)


def test_brief_drops_everything():
    rc, out = run("brief")
    assert rc == 0 and out == ""


def test_invalid_level_exits_zero():
    rc, _ = run("bogus")
    assert rc == 0


def test_downstream_close_exits_zero():
    p = subprocess.Popen(
        [sys.executable, VMKIT, "logfilter", "verbose"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    p.stdout.close()  # close the read end → child's writes get EPIPE
    try:
        p.stdin.write(SAMPLE.encode())
        p.stdin.close()
    except BrokenPipeError:
        pass
    assert p.wait(timeout=10) == 0


if __name__ == "__main__":
    for name in sorted(f for f in dir() if f.startswith("test_")):
        globals()[name]()
        print(f"PASS {name}")
    print("all tests passed")
