"""vmkit - 无头 VM 起停 + 串口操作 + base 镜像构建工具包。

能力:
  - 构建可独立启动的 Alpine raw 镜像(自定义包 + 烘焙文件 + ttyS0 autologin)
  - 从 base 镜像稀疏克隆起 N 个 VM,挂 QEMU socket,mcast 虚拟 L2 总线 / slirp
  - 经串口(unix socket)登录、跑命令、做断言(expect/send,无 mgmt NIC)
  - VM 组生命周期:launch / stop / pids

设计:rootless KVM(`/dev/kvm` 用户可写)、raw 盘(非 qcow2)、零 host 网络影响(无 host bridge/tap)。
非 Alpine 登录流程需自行适配 login_*;串口驱动本身 OS 无关。

用法见同目录 README.md。
"""
from __future__ import annotations
import os, re, select, socket, subprocess, time, signal, shutil, shlex, tempfile, urllib.parse, sys

# ---- .env 加载(机器/敏感配置;不覆盖已 export 的环境变量)----
def _load_env(start: str | None = None) -> None:
    """从 start(默认 CWD)向上查找 .env,把 KEY=VALUE 以 setdefault 注入 os.environ。
    无 .env 则无操作。供 VMKIT_ISO 等机器/敏感配置经 .env(.gitignore 排除)提供。"""
    d = os.path.abspath(start or os.getcwd())
    while True:
        p = os.path.join(d, ".env")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            except OSError:
                pass
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent


_load_env()

# ---- 默认值(可被环境变量/.env 覆盖)----
# ISO 路径必须显式提供:设 $VMKIT_ISO(.env 或 export)或 build_base(iso=...)。两者皆无则 build_base 报错。
DEFAULT_ISO = os.environ.get("VMKIT_ISO")
SLIRP_IP = "10.0.2.15"      # QEMU user-net 默认 guest IP
SLIRP_GW = "10.0.2.2"       # = host(供 guest wget host HTTP)
SLIRP_DNS = "10.0.2.3"

# apk 源:$VMKIT_APK_REPOS(逗号或空白分隔)覆盖;缺省 USTC v3.24 main+community。
# 换镜像/版本:改 .env 的 VMKIT_APK_REPOS(换版本改 URL 里的版本号即可)。常用镜像(以 v3.24 为例):
#   官方: https://dl-cdn.alpinelinux.org/alpine/v3.24/main,https://dl-cdn.alpinelinux.org/alpine/v3.24/community
#   USTC: https://mirrors.ustc.edu.cn/alpine/v3.24/main,https://mirrors.ustc.edu.cn/alpine/v3.24/community
#   阿里: https://mirrors.aliyun.com/alpine/v3.24/main,https://mirrors.aliyun.com/alpine/v3.24/community
#   清华: https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.24/main,https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.24/community
# 更多镜像见 https://mirrorz.org/site(各镜像可用性因网络而异,自行测试)。
DEFAULT_APK_REPOS = (
    [r for r in os.environ.get("VMKIT_APK_REPOS", "").replace(",", "\n").split() if r]
    or [
        "https://mirrors.ustc.edu.cn/alpine/v3.24/main",
        "https://mirrors.ustc.edu.cn/alpine/v3.24/community",
    ]
)


class Timeout(Exception):
    pass


# ===================== 串口驱动 =====================
class Console:
    """连一个 QEMU unix-socket 串口,提供 expect/send。"""

    def __init__(self, sock_path: str, log_path: str | None = None):
        self.sock_path = sock_path
        self.buf = bytearray()
        self.log = open(log_path, "ab") if log_path else None

    def connect(self, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.sock_path)
                s.setblocking(False)
                self.s = s
                return
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def _drain(self, timeout: float) -> bytes:
        end = time.monotonic() + timeout
        got = bytearray()
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([self.s], [], [], min(remaining, 0.5))
            if not r:
                if got:
                    break
                continue
            try:
                chunk = self.s.recv(8192)
            except BlockingIOError:
                continue
            if not chunk:
                break
            got += chunk
            if self.log:
                self.log.write(chunk); self.log.flush()
        if got:
            self.buf += got
        return bytes(got)

    def read_until(self, pattern, timeout: float = 30.0) -> bytes:
        """读到正则命中,返回命中前+命中段(并从 buf 移除)。超时抛 Timeout。"""
        if isinstance(pattern, str):
            pattern = pattern.encode("latin-1")
        rx = re.compile(pattern, re.DOTALL)
        end = time.monotonic() + timeout
        while True:
            m = rx.search(self.buf)
            if m:
                matched = bytes(self.buf[: m.end()])
                del self.buf[: m.end()]
                return matched
            if time.monotonic() > end:
                raise Timeout(f"timeout waiting for {pattern!r}; tail={bytes(self.buf[-300:])!r}")
            self._drain(0.5)

    def expect(self, pattern, timeout: float = 30.0) -> bytes:
        return self.read_until(pattern, timeout)

    def send(self, data) -> None:
        if isinstance(data, str):
            data = data.encode("latin-1")
        self.s.sendall(data)

    def sendline(self, line: str = "") -> None:
        self.send(line + "\n")

    def close(self):
        try: self.s.close()
        except Exception: pass
        if self.log: self.log.close()


# ===================== QEMU 启停 =====================
class QemuVM:
    """启动/停止一个 QEMU VM(KVM,raw 盘,串口走 unix socket)。"""

    def __init__(self, name: str, *, serial_sock: str, disks=None, cdrom: str | None = None,
                 nics=None, mem: int = 256, smp: int = 1, fw_cfg=None, extra=None,
                 log_path: str | None = None, qemu: str = "qemu-system-x86_64"):
        self.name = name
        self.serial_sock = serial_sock
        self.disks = disks or []
        self.cdrom = cdrom
        self.nics = nics or []          # [{netdev, mac?}]
        self.mem = mem
        self.smp = smp
        self.fw_cfg = fw_cfg or []      # [(name, val, "string"|"file")] -- QEMU 通用能力,按需注入
        self.extra = extra or []
        self.log_path = log_path
        self.qemu = qemu
        self.proc = None

    def _cmd(self) -> list[str]:
        c = [self.qemu, "-enable-kvm", "-cpu", "host",
             "-m", str(self.mem), "-smp", str(self.smp),
             "-display", "none", "-no-reboot", "-name", self.name,
             "-serial", f"unix:{self.serial_sock},server,nowait",
             "-monitor", "none"]
        for i, d in enumerate(self.disks):
            c += ["-drive", f"file={d},if=virtio,format=raw,index={i}"]
        if self.cdrom:
            c += ["-cdrom", self.cdrom, "-boot", "d"]
        for nic in self.nics:
            c += ["-netdev", nic["netdev"]]
            dev = f"virtio-net-pci,netdev={_netdev_id(nic['netdev'])}"
            if nic.get("mac"):
                dev += f",mac={nic['mac']}"
            c += ["-device", dev]
        for name, val, kind in self.fw_cfg:
            if kind == "string":
                c += ["-fw_cfg", f"name={name},string={val}"]
            else:
                c += ["-fw_cfg", f"name={name},file={val}"]
        c += self.extra
        return c

    def start(self):
        try: os.unlink(self.serial_sock)
        except FileNotFoundError: pass
        self.logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(self._cmd(), stdout=self.logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        return self.proc

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try: os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError: pass
            try: self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception: pass
        try: self.logf.close()
        except Exception: pass
        try: os.unlink(self.serial_sock)
        except FileNotFoundError: pass


def _netdev_id(netdev_spec: str) -> str:
    for part in netdev_spec.split(","):
        if part.startswith("id="):
            return part[3:]
    raise ValueError(f"netdev spec 缺 id=: {netdev_spec}")


# ===================== 串口 shell 辅助 =====================
def login_alpine(con: Console, boot_timeout: float = 120.0):
    """从串口登录 root shell 并设唯一提示符 SHELL>。
    兼容:base 镜像(ttyS0 autologin,直接出 :~#)、live ISO(出 login:)。"""
    con.send("\n")
    m = con.read_until(rb"(:~# |login: |SHELL> )", timeout=boot_timeout)
    if m.rstrip().endswith(b"login:"):
        con.sendline("root")
        try:
            con.read_until(rb"# ", timeout=10)
        except Timeout:
            con.read_until(rb"Password: ", timeout=3)
            con.sendline("")
            con.read_until(rb"# ", timeout=10)
    con.sendline("set +o emacs 2>/dev/null; set +o vi 2>/dev/null; export PS1='SHELL> '")
    con.read_until(rb"SHELL> ", timeout=5)


def clean(out_bytes) -> str:
    """去掉命令回显(第一行,含 SHELL> 提示与命令本身)+ ANSI 转义,保留实际输出。"""
    s = out_bytes.decode("latin-1", "replace") if isinstance(out_bytes, (bytes, bytearray)) else out_bytes
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)
    idx = s.find("\n")
    s = s[idx + 1:] if idx >= 0 else s
    return s.strip()


def run_cmd(con: Console, cmd: str, timeout: float = 20.0):
    """发一条命令,等到自定义 RC marker,返回 (cleaned_output_str, exitcode)。
    用时间戳 marker 防撞车。注意:必须在 bytes(con.buf) 副本上 search--
    re.search(bytearray) 的 group 别名到原 bytearray,随后 del 会使 group 失效变空。"""
    marker = f"__RC{int(time.monotonic() * 1000)}__"
    con.sendline(f"{cmd}; echo {marker}=$?")
    rx = re.compile((marker + r"=(\d+)").encode())
    end = time.monotonic() + timeout
    while True:
        m = rx.search(bytes(con.buf))
        if m:
            out = bytes(con.buf[: m.start()])
            del con.buf[: m.end()]
            rc = int(m.group(1)) if m.group(1) else -1
            return clean(out), rc
        if time.monotonic() > end:
            tail = bytes(con.buf[-300:])
            del con.buf[:]   # 超时:清空残留,避免污染下次 run_cmd 的输出
            raise Timeout(f"cmd timeout: {cmd!r}; tail={tail!r}")
        con._drain(0.5)


# ===================== base 镜像构建 =====================
def build_base(*, out_raw: str, packages, bake_files=None, iso: str = DEFAULT_ISO,
               apk_repos: list | None = None, mem: int = 1536, smp: int = 2,
               size_gb: int = 2, http_port: int = 8080):
    """构建可独立启动的 Alpine raw 镜像(含 ttyS0 autologin,供 VMGroup 起 VM)。

    packages:apk 包名列表(如 ["frr","dnsmasq",...])。
    bake_files:[(host_path, guest_path), ...] -- 额外文件烘焙进镜像(lbu 不备份的非 /etc 文件,
                如二进制);经 host HTTP 投递 + setup-disk 后 post-copy 到 guest_path。

    机制:live ISO -> 静态 slirp -> apk update -> setup-disk -m sys -> apk add --root /mnt <packages>
    (lbu overlay 不带 world 额外包,故包改在 setup-disk 后用 --root 装)-> 烘焙 ttyS0 autologin +
    tun 模块 + bake_files -> poweroff。
    """
    if not iso:
        raise ValueError("未指定 Alpine ISO:设置 $VMKIT_ISO 环境变量,或传 iso= 参数")
    apk_repos = apk_repos or DEFAULT_APK_REPOS
    bake_files = bake_files or []
    # shell-安全:插进串口 shell 命令的用户输入(repos/pkgs/路径)一律 quote
    repos_args = " ".join(shlex.quote(r) for r in apk_repos)   # 供 printf '%s\n' <args>
    pkgs_args = " ".join(shlex.quote(p) for p in packages)
    # staging dir:host HTTP 投递 bake_files
    staging = tempfile.mkdtemp(prefix="vmkit-build-")
    try:
        staged = []  # [(basename, guest_path)]
        for host_path, guest_path in bake_files:
            shutil.copy(host_path, os.path.join(staging, os.path.basename(host_path)))
            staged.append((os.path.basename(host_path), guest_path))
        httpd = subprocess.Popen(["python3", "-m", "http.server", str(http_port), "--bind", "127.0.0.1"],
                                 cwd=staging, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        if httpd.poll() is not None:
            raise RuntimeError(f"http.server 启动失败(端口 {http_port} 被占用?);httpd 已退出")
        # 建稀疏盘
        d = os.path.dirname(out_raw)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(out_raw):
            os.unlink(out_raw)
        with open(out_raw, "wb") as f:
            f.truncate(size_gb * 1024 ** 3)

        sock = "/tmp/vmkit-build.sock"
        vm = QemuVM("vmkit-build", serial_sock=sock, cdrom=iso, disks=[out_raw],
                    nics=[{"netdev": "user,id=n0"}], mem=mem, smp=smp,
                    log_path=out_raw + ".qemu.log")
        print(f">> building {out_raw} (iso={iso}, {len(packages)} pkgs, {len(bake_files)} bake_files)", flush=True)
        vm.start()
        try:
            con = Console(sock, log_path=out_raw + ".console.log")
            con.connect(30)
            login_alpine(con, 150)
            print("== build shell ok ==", flush=True)

            def step(name, cmd, to=60):
                out, rc = run_cmd(con, cmd, timeout=to)
                print(f">>> {name} [rc={rc}]\n{out}", flush=True)
                return rc

            step("static slirp",
                 f"ip link set eth0 up; ip addr add {SLIRP_IP}/24 dev eth0; "
                 f"ip route add default via {SLIRP_GW}; "
                 f"printf 'nameserver {SLIRP_DNS}\\n' > /etc/resolv.conf; ip addr show eth0 | grep 'inet '", 15)
            step("apk repos + update",
                 f"printf '%s\\n' {repos_args} > /etc/apk/repositories; apk update 2>&1 | tail -2", 120)
            # 投递 bake_files 到 live(后续 post-copy 到目标盘)
            for base, _gpath in staged:
                bpath = shlex.quote(base)                    # 本地路径
                burl = urllib.parse.quote(base, safe="")     # URL path
                step(f"wget {base}",
                     f"wget -q --timeout=60 -O /tmp/{bpath} http://{SLIRP_GW}:{http_port}/{burl} && "
                     f"chmod +x /tmp/{bpath}; ls -l /tmp/{bpath}", 90)
            # ttyS0 autologin:busybox getty 无 -a,用 -n -l <autologin 脚本>(exec login -f root)
            step("inittab autologin",
                 "printf '#!/bin/sh\\nexec /bin/login -f root\\n' > /etc/autologin.sh && chmod +x /etc/autologin.sh; "
                 "sed -i 's|getty -L 0 ttyS0|getty -n -l /etc/autologin.sh -L 0 ttyS0|' /etc/inittab; "
                 "grep ttyS0 /etc/inittab", 10)
            step("tun module", "echo tun >> /etc/modules; modprobe tun 2>&1; ls -l /dev/net/tun 2>&1", 15)
            step("interfaces loopback",
                 "printf 'auto lo\\niface lo inet loopback\\n' > /etc/network/interfaces; cat /etc/network/interfaces", 10)
            step("rc-update",
                 "rc-update add networking boot 2>&1; rc-update add sshd default 2>&1; "
                 "rc-update show default | grep -E 'sshd'", 15)
            step("setup-disk -m sys",
                 "export ERASE_DISKS=/dev/vda SWAP_SIZE=128 BOOTLOADER=syslinux; setup-disk -m sys /dev/vda 2>&1 | tail -20", 600)
            # setup-disk 后:apk add --root /mnt 装包 + post-copy bake_files + autologin
            postcmd = (
                "for p in /dev/vda3 /dev/vda2 /dev/vda1; do "
                "[ -b \"$p\" ] || continue; mount $p /mnt 2>/dev/null || continue; "
                "if [ -f /mnt/etc/inittab ]; then "
                f"printf '%s\\n' {repos_args} > /mnt/etc/apk/repositories; "
                "cp /etc/resolv.conf /mnt/etc/resolv.conf; "
                "mkdir -p /mnt/etc/apk/keys; cp /etc/apk/keys/* /mnt/etc/apk/keys/ 2>/dev/null; "
                f"apk add --root /mnt --no-cache {pkgs_args} 2>&1 | tail -4; "
                + "".join(
                    f"cp /tmp/{shlex.quote(b)} /mnt{shlex.quote(g)}; chmod +x /mnt{shlex.quote(g)}; "
                    for b, g in staged)
                + "cp /etc/autologin.sh /mnt/etc/autologin.sh 2>/dev/null; "
                "chmod +x /mnt/etc/autologin.sh 2>/dev/null; "
                "echo VERIFY:; ls /mnt/usr/local/bin 2>/dev/null | head; "
                "umount /mnt; break; fi; umount /mnt 2>/dev/null; done"
            )
            step("post-install apk add --root + copy bake_files", postcmd, 400)
            step("poweroff", "poweroff", 10)
            time.sleep(4)
        finally:
            vm.kill()
            httpd.terminate()
            try: httpd.wait(timeout=5)
            except Exception: pass
        print(f">> built {out_raw} ({os.path.getsize(out_raw) // 1024 // 1024} MiB virtual)", flush=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ===================== VM 组生命周期 =====================
class VMGroup:
    """从 base 镜像稀疏克隆起一组 VM,挂虚拟总线,串口编排。"""

    def __init__(self, base_raw: str, disks_dir: str, logs_dir: str):
        self.base_raw = base_raw
        self.disks_dir = disks_dir
        self.logs_dir = logs_dir
        os.makedirs(disks_dir, exist_ok=True)
        os.makedirs(logs_dir, exist_ok=True)
        self.vms: dict[str, QemuVM] = {}

    def launch(self, vms: dict):
        """vms: {name: {'mem','smp','nics':[(netdev,mac)]}}。"""
        assert os.path.exists(self.base_raw), f"missing {self.base_raw}; build first"
        pidfile = os.path.join(self.logs_dir, "vms.pid")
        for name, cfg in vms.items():
            disk = os.path.join(self.disks_dir, f"{name}.raw")
            if os.path.exists(disk):
                os.unlink(disk)
            subprocess.run(["cp", "--sparse=always", self.base_raw, disk], check=True)
            nics = [{"netdev": nd, "mac": mac} for nd, mac in cfg["nics"]]
            sock = os.path.join(self.logs_dir, f"{name}.sock")
            vm = QemuVM(name, serial_sock=sock, disks=[disk], nics=nics,
                        mem=cfg.get("mem", 256), smp=cfg.get("smp", 1),
                        log_path=os.path.join(self.logs_dir, f"{name}.qemu.log"))
            vm.start()
            self.vms[name] = vm
            print(f"  {name:12s} pid={vm.proc.pid} mem={cfg.get('mem',256)} nics={len(nics)}", flush=True)
        with open(pidfile, "w") as f:
            for name, vm in self.vms.items():
                f.write(f"{name} {vm.proc.pid}\n")
        print(f">> {len(self.vms)} VMs launched. socks: {self.logs_dir}/<name>.sock", flush=True)

    def stop(self):
        killed = []
        for name, vm in self.vms.items():
            vm.kill(); killed.append(name)
        # 兜底:按 pidfile + 名字杀残留
        pidfile = os.path.join(self.logs_dir, "vms.pid")
        if os.path.exists(pidfile):
            for line in open(pidfile):
                parts = line.split()
                if len(parts) >= 2:
                    try: os.kill(int(parts[1]), signal.SIGTERM)
                    except ProcessLookupError: pass
        time.sleep(2)
        # 兜底:按本组实际起过的 VM 名 pkill 残留(通用,不写死用例名)
        names = list(self.vms)
        if names:
            # 名字后加边界(空格/逗号/行尾),避免 vm1 误杀 vm10 等前缀碰撞
            pat = r"qemu-system-x86_64.*-name (" + "|".join(re.escape(n) for n in names) + r")( |,|$)"
            subprocess.run(["pkill", "-9", "-f", pat], stderr=subprocess.DEVNULL)
        for name in names:
            for p in (os.path.join(self.disks_dir, f"{name}.raw"),
                      os.path.join(self.logs_dir, f"{name}.sock")):
                try: os.unlink(p)
                except FileNotFoundError: pass
        try: os.unlink(pidfile)
        except FileNotFoundError: pass
        print(f">> stopped {killed}; disks/socks cleaned", flush=True)


# ===================== CLI =====================
def _cli():
    """python3 vmkit.py exec <sock> '<cmd>'  -- 对运行中 VM 跑一条命令。"""
    if len(sys.argv) >= 4 and sys.argv[1] == "exec":
        sock, cmd = sys.argv[2], sys.argv[3]
        con = Console(sock, log_path=None); con.connect(10)
        login_alpine(con, 60)
        out, rc = run_cmd(con, cmd, timeout=30)
        print(out); print(f"[rc={rc}]")
        con.close()
        return
    print(__doc__)
    print("\nCLI: python3 vmkit.py exec <sock> '<cmd>'")


if __name__ == "__main__":
    _cli()
