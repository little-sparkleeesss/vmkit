#!/usr/bin/env python3
"""vmkit 冒烟测试(不依赖 KVM/VM)。运行: python3 tests/test_smoke.py

覆盖:向后兼容 API 面 + v2 泛化(盘格式/hostfwd/machine/daemonize/SshConsole/build 注册表/CLI)。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import vmkit  # noqa: E402


def check(name, cond):
    assert cond, f"FAIL: {name}"
    print(f"  ok  {name}")


print("== 向后兼容 API 面 ==")
for n in ("Console", "QemuVM", "VMGroup", "SshConsole",
          "login_alpine", "run_cmd", "build_base", "register_builder"):
    check(f"has {n}", hasattr(vmkit, n))

import inspect  # noqa: E402
sig = inspect.signature(vmkit.build_base)
check("build_base distro 缺省=alpine", sig.parameters["distro"].default == "alpine")
sa = inspect.signature(vmkit.build_alpine)
check("build_alpine 保留旧参", all(p in sa.parameters for p in ("out_raw", "packages", "apk_repos")))

print("== QemuVM 泛化:旧式缺省=现状 ==")
old = vmkit.QemuVM("v", serial_sock="/tmp/v.sock", disks=["/d/a.raw"],
                   nics=[{"netdev": "user,id=n0"}])
c = " ".join(old._cmd())
check("旧式 default raw", "format=raw" in c)
check("旧式 unix 串口", "unix:/tmp/v.sock,server,nowait" in c)
check("旧式无 -machine", "-machine" not in c)
check("旧式无 -daemonize", "-daemonize" not in c)

print("== QemuVM 泛化:新式能力 ==")
new = vmkit.QemuVM("v2", disks=[{"path": "/o.qcow2", "format": "qcow2"}],
                   nics=[{"netdev": "user,id=n0", "hostfwd": "hostfwd=tcp:127.0.0.1:2222-:22"}],
                   machine="q35", daemonize=True, pidfile="/p.pid", serial="file:/s")
c = " ".join(new._cmd())
check("qcow2 盘", "format=qcow2" in c)
check("hostfwd", "hostfwd=tcp:127.0.0.1:2222-:22" in c)
check("q35", "-machine q35" in c)
check("daemonize+pidfile", "-daemonize" in c and "-pidfile /p.pid" in c)
check("serial file", "file:/s" in c)

print("== SshConsole 默认严格校验 ==")
s = vmkit.SshConsole("h")
check("默认 StrictHostKeyChecking=yes", any(o == "StrictHostKeyChecking=yes" for o in s.opts))
s2 = vmkit.SshConsole("h", strict_host_checking=False)
check("非严格=no+/dev/null",
      any(o == "StrictHostKeyChecking=no" for o in s2.opts)
      and any(o == "UserKnownHostsFile=/dev/null" for o in s2.opts))
ssh_args = s2._ssh("uname -r")
check("ssh 参数顺序: destination 在 command 前",
      ssh_args[-2] == "root@h" and ssh_args[-1] == "uname -r")

print("== build_base 可插拔 ==")
check("注册 alpine", "alpine" in vmkit.BUILDERS)
check("注册 fedora-cloud", "fedora-cloud" in vmkit.BUILDERS)
try:
    vmkit.build_base(distro="nope")
    check("未注册分发报错", False)
except ValueError:
    check("未注册分发报错", True)

print("== CLI ==")
here = os.path.join(os.path.dirname(__file__), "..", "vmkit.py")


def cli(*args):
    return subprocess.run([sys.executable, here, *args],
                          capture_output=True, text=True)


r = cli("--help")
check("--help exit 0", r.returncode == 0)
r = cli("qemu", "status", "x", "--pidfile", "/tmp/definitely-none.pid")
check("qemu status 缺失 pidfile exit 1", r.returncode == 1)
r = cli("status", "--dir", "/tmp")
check("status --dir exit 0", r.returncode == 0)
r = cli("exec", "--ssh", "--help")
check("exec --ssh 帮助 exit 0", r.returncode == 0)

print("== 生命周期清理 ==")
import tempfile  # noqa: E402
with tempfile.NamedTemporaryFile("w", delete=False) as f:
    f.write("not-a-pid\n")
    pf = f.name
try:
    vm = vmkit.QemuVM("kill-test", daemonize=True, pidfile=pf)
    vm.kill()
    check("kill 后清理 pidfile", not os.path.exists(pf))
finally:
    if os.path.exists(pf):
        os.unlink(pf)

print("\n全部通过")
