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
    """启动/停止一个 QEMU VM(KVM;盘格式可配;串口走 unix socket 或 file)。

    向后兼容:缺省行为与旧版一致(raw 盘、virtio NIC、unix 串口、前台 Popen)。
    泛化(全部可选,带默认值):
      - disks: [path,...](全部 raw) 或 [{"path","format","if","index","extra"?},...] 混合;
        format=qcow2 的覆盖盘由 QEMU 自动读 backing。
      - machine: 如 "q35";None 则不传。
      - serial: 完整 -serial 参数(如 "file:/x.log");None 则由 serial_sock 拼 unix。
      - nics[].hostfwd: 追加进 user netdev(如 "hostfwd=tcp:127.0.0.1:2222-:22")。
      - daemonize/pidfile: 加 -daemonize -pidfile;is_alive/kill 走 pidfile。
      - kvm/cpu/no_reboot/boot/display: 可配,缺省=现状。
    """

    def __init__(self, name: str, *, serial_sock: str | None = None, disks=None,
                 cdrom: str | None = None, nics=None, mem: int = 256, smp: int = 1,
                 fw_cfg=None, extra=None, log_path: str | None = None,
                 qemu: str = "qemu-system-x86_64",
                 machine: str | None = None, serial: str | None = None,
                 daemonize: bool = False, pidfile: str | None = None,
                 kvm: bool = True, cpu: str = "host", no_reboot: bool = True,
                 boot: str | None = None, display: str = "none"):
        self.name = name
        self.serial_sock = serial_sock
        self.disks = disks or []
        self.cdrom = cdrom
        self.nics = nics or []          # [{netdev, mac?, hostfwd?}]
        self.mem = mem
        self.smp = smp
        self.fw_cfg = fw_cfg or []      # [(name, val, "string"|"file")] -- QEMU 通用能力,按需注入
        self.extra = extra or []
        self.log_path = log_path
        self.qemu = qemu
        self.machine = machine
        self.serial = serial
        self.daemonize = daemonize
        self.pidfile = pidfile
        self.kvm = kvm
        self.cpu = cpu
        self.no_reboot = no_reboot
        self.boot = boot
        self.display = display
        self.proc = None
        self.logf = None

    @classmethod
    def from_spec(cls, name: str, spec: dict):
        """从 spec dict 构造(供 CLI `qemu start <name> <spec.json>` 等用)。"""
        keys = ("disks", "cdrom", "nics", "mem", "smp", "fw_cfg", "extra",
                "log_path", "qemu", "machine", "serial", "daemonize", "pidfile",
                "kvm", "cpu", "no_reboot", "boot", "display")
        kw = {k: spec[k] for k in keys if k in spec}
        return cls(name, serial_sock=spec.get("serial_sock"), **kw)

    def _serial_arg(self) -> str:
        if self.serial is not None:
            return self.serial
        if self.serial_sock:
            return f"unix:{self.serial_sock},server,nowait"
        return "none"

    @staticmethod
    def _drive_arg(i: int, d) -> str:
        """d 为路径字符串(raw)或 dict(path/format/if|bus/index/extra)。"""
        if isinstance(d, str):
            d = {"path": d}
        path = d["path"]
        fmt = d.get("format", "raw")
        iface = d.get("if", d.get("bus", "virtio"))
        index = d.get("index", i)
        s = f"file={path},if={iface},format={fmt},index={index}"
        extra = d.get("extra")
        if extra:
            s += f",{extra}"
        return s

    def _cmd(self) -> list[str]:
        c = [self.qemu]
        if self.kvm:
            c += ["-enable-kvm"]
        c += ["-cpu", self.cpu, "-m", str(self.mem), "-smp", str(self.smp),
              "-display", self.display]
        if self.no_reboot:
            c += ["-no-reboot"]
        c += ["-name", self.name, "-serial", self._serial_arg(), "-monitor", "none"]
        if self.machine:
            c += ["-machine", self.machine]
        for i, d in enumerate(self.disks):
            c += ["-drive", self._drive_arg(i, d)]
        if self.cdrom:
            c += ["-cdrom", self.cdrom]
        boot = self.boot or ("d" if self.cdrom else None)
        if boot:
            c += ["-boot", boot]
        for nic in self.nics:
            netdev = nic["netdev"]
            hf = nic.get("hostfwd")
            if hf:
                netdev = f"{netdev},{hf}"
            c += ["-netdev", netdev]
            dev = f"virtio-net-pci,netdev={_netdev_id(netdev)}"
            if nic.get("mac"):
                dev += f",mac={nic['mac']}"
            c += ["-device", dev]
        for name, val, kind in self.fw_cfg:
            if kind == "string":
                c += ["-fw_cfg", f"name={name},string={val}"]
            else:
                c += ["-fw_cfg", f"name={name},file={val}"]
        if self.daemonize:
            c += ["-daemonize"]
        if self.pidfile:
            c += ["-pidfile", self.pidfile]
        c += self.extra
        return c

    def _read_pid(self) -> int | None:
        if not self.pidfile:
            return None
        try:
            with open(self.pidfile) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def start(self):
        if self.serial_sock:
            try: os.unlink(self.serial_sock)
            except FileNotFoundError: pass
        if self.pidfile:
            try: os.unlink(self.pidfile)
            except FileNotFoundError: pass
        self.logf = open(self.log_path, "ab") if self.log_path else None
        self.proc = subprocess.Popen(self._cmd(),
                                     stdout=self.logf or subprocess.DEVNULL,
                                     stderr=subprocess.STDOUT if self.logf else subprocess.DEVNULL,
                                     start_new_session=True)
        if self.daemonize and self.pidfile:
            # -daemonize 会二次 fork,pidfile 稍后才出现;等它(或 launcher 提前退出=失败)
            for _ in range(50):
                if self._read_pid() is not None:
                    break
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        return self.proc

    def is_alive(self) -> bool:
        pid = self._read_pid()
        if self.daemonize and pid is not None:
            try:
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        pid = self._read_pid()
        if self.daemonize and pid is not None:
            try: os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError): pass
            try: os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError): pass
        elif self.proc and self.proc.poll() is None:
            try: os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except ProcessLookupError: pass
        for _ in range(20):            # 等退出(最多 5s)
            if not self.is_alive():
                break
            time.sleep(0.25)
        if self.is_alive():
            pid2 = self._read_pid() or (self.proc.pid if self.proc else None)
            if pid2:
                try: os.kill(pid2, signal.SIGKILL)
                except (ProcessLookupError, PermissionError): pass
        try: self.logf.close()
        except Exception: pass
        if self.serial_sock:
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


# ===================== SSH 编排 =====================
class SshConsole:
    """SSH 编排(经 QEMU hostfwd 进 guest 的 sshd),与串口 Console 并存。

    供 Fedora 等非 Alpine 发行版使用(如 zfs-compat 的 SSH+hostfwd 编排)。
    依赖 host 上的 ssh/scp。串口路径仍由 Console/login_alpine/run_cmd 负责。
    """

    def __init__(self, host: str, *, port: int = 22, user: str = "root",
                 keyfile: str | None = None, timeout: float = 20.0,
                 strict_host_checking: bool = True):
        """strict_host_checking: 默认 True(校验 host key)。
        仅对本地一次性测试 VM(如 build_fedora_cloud 的 localhost)显式传 False。"""
        self.host = host
        self.port = port
        self.user = user
        self.keyfile = keyfile
        self.timeout = timeout
        self.strict_host_checking = strict_host_checking
        self.opts = ["-o", "ConnectTimeout=10"]
        if strict_host_checking:
            self.opts += ["-o", "StrictHostKeyChecking=yes",
                          "-o", f"UserKnownHostsFile={os.path.expanduser('~/.ssh/known_hosts')}"]
        else:
            self.opts += ["-o", "StrictHostKeyChecking=no",
                          "-o", "UserKnownHostsFile=/dev/null"]
        if keyfile:
            self.opts += ["-i", keyfile]
        self.dest = f"{user}@{host}"

    def _ssh(self, cmd: str) -> list[str]:
        return ["ssh"] + self.opts + ["-p", str(self.port), cmd, self.dest]

    def connect(self, timeout: float | None = None):
        """等 SSH 可连(带退避);超时抛 Timeout。"""
        t = timeout or self.timeout
        deadline = time.monotonic() + t
        while True:
            try:
                if self.run("true", timeout=5)[1] == 0:
                    return
            except Timeout:
                pass
            if time.monotonic() > deadline:
                raise Timeout(f"ssh connect timeout to {self.dest}:{self.port}")
            time.sleep(1)

    def run(self, cmd: str, timeout: float | None = None,
            check: bool = False) -> tuple[str, int]:
        """跑一条命令,返回 (去尾部空白输出, exitcode)。"""
        to = timeout or self.timeout
        try:
            p = subprocess.run(self._ssh(cmd), capture_output=True, text=True, timeout=to)
        except subprocess.TimeoutExpired:
            raise Timeout(f"ssh cmd timeout: {cmd!r}")
        out = (p.stdout or "").rstrip("\n")
        if check and p.returncode != 0:
            raise RuntimeError(f"ssh cmd failed rc={p.returncode}: {cmd!r}\n{out}")
        return out, p.returncode

    def scp_send(self, local: str, remote: str):
        subprocess.run(["scp"] + self.opts + ["-P", str(self.port), local,
                                              f"{self.dest}:{remote}"], check=True)

    def scp_recv(self, remote: str, local: str):
        subprocess.run(["scp"] + self.opts + ["-P", str(self.port),
                                              f"{self.dest}:{remote}", local], check=True)


# ===================== base 镜像构建(可插拔) =====================
# 发行版构建器注册表:register_builder(distro, fn);build_base(distro=...) 分发。
# 缺省 "alpine" = 旧 build_base 行为(向后兼容)。
BUILDERS: dict[str, "Callable[..., None]"] = {}


def register_builder(distro: str, fn) -> None:
    BUILDERS[distro] = fn


def build_base(*, distro: str = "alpine", **kw):
    """分发到注册的发行版构建器。缺省 alpine(向后兼容)。

    注册新发行版:def my_builder(*, out_xxx, ...): ...; vmkit.register_builder("myos", my_builder)
    """
    if distro not in BUILDERS:
        raise ValueError(f"未注册的发行版构建器: {distro!r}; 已注册: {sorted(BUILDERS)}")
    return BUILDERS[distro](**kw)


def build_alpine(*, out_raw: str, packages, bake_files=None, iso: str = DEFAULT_ISO,
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


def build_fedora_cloud(*, out_qcow2: str, release: int, arch: str = "x86_64",
                       host_port: int = 2222, ssh_user: str = "zfsbuild",
                       ssh_keyfile: str | None = None, provision_script: str | None = None,
                       mem: int = 4096, smp: int = 4):
    """下载 Fedora Cloud Base qcow2 + cloud-init 注入 ssh key + 跑 provision 后关机。

    参考实现(演示 build_base 可插拔,注册名 "fedora-cloud"):适合用 cloud-init 的发行版,
    产出可复用 qcow2(类似 zfs-compat 的 prep,但走 vmkit 的 QemuVM/SshConsole)。
    需要 host 的 mkisofs/genisoimage。
    """
    import urllib.request
    image_dir = (f"https://download.fedoraproject.org/pub/fedora/linux/releases/"
                 f"{release}/Cloud/{arch}/images/")
    d = os.path.dirname(out_qcow2)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="vmkit-fedora-")
    try:
        # 1) 目录里找最新 Generic qcow2
        listing = urllib.request.urlopen(image_dir, timeout=60).read().decode()
        names = sorted(set(re.findall(r'Fedora-Cloud-Base-Generic-[^"]*\.qcow2', listing)))
        if not names:
            raise ValueError(f"目录里找不到 Generic qcow2: {image_dir}")
        name = names[-1]
        print(f">> 下载 {image_dir}{name} ...", flush=True)
        img = os.path.join(tmp, name)
        urllib.request.urlretrieve(image_dir + name, img)

        # 2) ssh 密钥(缺省自动生成)
        key = ssh_keyfile
        if key is None:
            key = os.path.join(tmp, "id")
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key, "-q"], check=True)
        with open(key + ".pub") as f:
            pub = f.read().strip()

        # 3) cloud-init NoCloud seed
        seed = os.path.join(tmp, "seed")
        os.makedirs(seed)
        with open(os.path.join(seed, "user-data"), "w") as f:
            f.write("#cloud-config\n"
                    f"users:\n  - name: {ssh_user}\n    groups: wheel\n"
                    f"    sudo: \"ALL=(ALL) NOPASSWD: ALL\"\n"
                    f"    ssh_authorized_keys:\n      - {pub}\n"
                    "ssh_pwauth: false\ndisable_root: true\n")
        with open(os.path.join(seed, "meta-data"), "w") as f:
            f.write("instance-id: vmkit-fedora\nlocal-hostname: vmkit-fedora\n")
        mkiso = next((x for x in ("mkisofs", "genisoimage")
                      if shutil.which(x)), None)
        if not mkiso:
            raise RuntimeError("需要 host 装 mkisofs 或 genisoimage 生成 cloud-init seed ISO")
        iso = os.path.join(tmp, "seed.iso")
        if subprocess.run([mkiso, "-quiet", "-o", iso, "-V", "cidata", "-R", "-J", seed]).returncode:
            raise RuntimeError(f"{mkiso} 生成 seed ISO 失败")

        # 4) 起 VM(hostfwd 进 SSH),跑 provision,关机
        shutil.copy(img, out_qcow2)
        vm = QemuVM("vmkit-fedora-build",
                    disks=[{"path": out_qcow2, "format": "qcow2"}],
                    cdrom=iso,
                    nics=[{"netdev": "user,id=n0",
                           "hostfwd": f"hostfwd=tcp:127.0.0.1:{host_port}-:22"}],
                    mem=mem, smp=smp, machine="q35",
                    log_path=out_qcow2 + ".qemu.log")
        vm.start()
        try:
            con = SshConsole("127.0.0.1", port=host_port, user=ssh_user,
                             keyfile=key, timeout=30, strict_host_checking=False)
            print("== 等 SSH(cloud-init 首次启动,最多 10 分钟)...", flush=True)
            con.connect(timeout=600)
            print("== SSH ok", flush=True)
            if provision_script:
                con.scp_send(provision_script, "/tmp/provision.sh")
                out, _ = con.run("sudo bash /tmp/provision.sh", timeout=1800, check=True)
                print(out)
            con.run("sudo poweroff", timeout=10)
        finally:
            vm.kill()
        print(f">> built {out_qcow2}", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# 注册内建构建器
register_builder("alpine", build_alpine)
register_builder("fedora-cloud", build_fedora_cloud)


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
def _cli() -> int:
    import argparse, json
    argv = sys.argv[1:]

    # 向后兼容快路径:python3 vmkit.py exec <sock> '<cmd>' (串口)
    if len(argv) >= 2 and argv[0] == "exec" and not argv[1].startswith("--"):
        if len(argv) < 3:
            print("用法: vmkit.py exec <sock> '<cmd>'", file=sys.stderr)
            return 2
        sock, cmd = argv[1], argv[2]
        con = Console(sock, log_path=None); con.connect(10)
        login_alpine(con, 60)
        out, rc = run_cmd(con, cmd, timeout=30)
        print(out); print(f"[rc={rc}]")
        con.close()
        return 0

    ap = argparse.ArgumentParser(prog="vmkit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # qemu start/stop/status(zfs-compat 调用面)
    p = sub.add_parser("qemu", help="QEMU VM 生命周期(qcow2/hostfwd/q35 等 via spec json)")
    qsub = p.add_subparsers(dest="qcmd", required=True)
    qs = qsub.add_parser("start"); qs.add_argument("name"); qs.add_argument("spec")
    qstop = qsub.add_parser("stop"); qstop.add_argument("name"); qstop.add_argument("--pidfile")
    qstat = qsub.add_parser("status"); qstat.add_argument("name"); qstat.add_argument("--pidfile")

    # VMGroup
    lp = sub.add_parser("launch", help="从 group.json 起一组 VM")
    lp.add_argument("group")
    sp = sub.add_parser("stop", help="停掉 group.json 对应的一组 VM")
    sp.add_argument("group")

    # build
    bp = sub.add_parser("build", help="build_base(distro=...) 分发;spec 为构建器参数 JSON")
    bp.add_argument("distro"); bp.add_argument("spec")

    # status:扫描目录下的 *.pid
    st = sub.add_parser("status", help="扫描 pidfile 目录列出运行中 VM")
    st.add_argument("--dir", default=".")

    # exec --ssh <host> <cmd>
    es = sub.add_parser("exec", help="exec --ssh [--port] [--user] [--key] [--insecure] <host> '<cmd>'")
    es.add_argument("--ssh", action="store_true")
    es.add_argument("--port", type=int, default=22)
    es.add_argument("--user", default="root")
    es.add_argument("--key")
    es.add_argument("--insecure", action="store_true",
                    help="关闭 host key 校验(仅限可信/一次性环境)")
    es.add_argument("host"); es.add_argument("cmd")

    ns = ap.parse_args(argv)

    if ns.cmd == "qemu":
        if ns.qcmd == "start":
            with open(ns.spec) as f:
                spec = json.load(f)
            vm = QemuVM.from_spec(ns.name, spec)
            vm.start()
            print(vm.pidfile or (vm.proc.pid if vm.proc else "started"))
            return 0
        name = ns.name
        pidfile = ns.pidfile or f"{name}.pid"
        vm = QemuVM(name, daemonize=True, pidfile=pidfile)
        if ns.qcmd == "stop":
            vm.kill()
            return 0
        # status
        if vm.is_alive():
            print("running")
            return 0
        print("stopped")
        return 1

    if ns.cmd == "launch":
        with open(ns.group) as f:
            g = json.load(f)
        grp = VMGroup(g["base_raw"], g["disks_dir"], g["logs_dir"])
        grp.launch(g.get("vms", {}))
        return 0

    if ns.cmd == "stop":
        with open(ns.group) as f:
            g = json.load(f)
        grp = VMGroup(g["base_raw"], g["disks_dir"], g["logs_dir"])
        grp.stop()
        return 0

    if ns.cmd == "build":
        with open(ns.spec) as f:
            spec = json.load(f)
        build_base(distro=ns.distro, **spec)
        return 0

    if ns.cmd == "status":
        alive = []
        for name in sorted(os.listdir(ns.dir)):
            if not name.endswith(".pid"):
                continue
            path = os.path.join(ns.dir, name)
            try:
                pid = int(open(path).read().strip())
                os.kill(pid, 0)
                alive.append((name, pid))
            except (OSError, ValueError):
                pass
        for name, pid in alive:
            print(f"{name} {pid}")
        return 0

    if ns.cmd == "exec" and ns.ssh:
        s = SshConsole(ns.host, port=ns.port, user=ns.user, keyfile=ns.key,
                       strict_host_checking=not ns.insecure)
        out, rc = s.run(ns.cmd)
        print(out); print(f"[rc={rc}]")
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
