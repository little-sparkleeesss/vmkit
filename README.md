# vmkit - 无头 KVM VM 工具包(发行版无关)

单文件 Python 库(`vmkit.py`,零依赖、零打包)。在 Linux host 上用 **rootless KVM + QEMU** 构建 base 镜像、
起若干无头 VM,经**串口**(Alpine 等无 sshd 环境)或 **SSH**(Fedora 等经 hostfwd)无头编排。
零 host 网络影响(无 host bridge/tap、无 root)。

## 如何复用(他处使用)
vmkit 是单文件库,三种方式任选:
- **拷贝** `vmkit.py` 到你的项目,`import vmkit` 即可;
- 把本目录加入 `PYTHONPATH`(或 `sys.path.insert`),再 `import vmkit`;
- 或作为 **git submodule** 引入(见各项目自身说明)。

## 配置(.env)
机器/敏感配置(如 ISO 路径)经 `.env` 提供,**不入库**:
1. `cp .example.env .env`,填入本机实际值;
2. vmkit **导入时自动**从 CWD 向上查找 `.env` 并加载(`os.environ.setdefault`,不覆盖已 export 的变量)。

可配置项(见 `.example.env`):
- `VMKIT_ISO` -- Alpine virt ISO 路径(`build_base` **必填**)。
- `VMKIT_APK_REPOS` -- apk 源 URL 列表(逗号或空白分隔);缺省 USTC v3.24 main+community。
  换镜像/版本改这里(URL 含版本号,换版本改版本号)。常用镜像(官方/USTC/阿里/清华)见 `.example.env` 注释。

也可直接 `export` 这些变量;`.env` 仅在变量未设置时补值。

## 能力
- **构建 base 镜像**(`build_base`):**可插拔发行版构建器**(`register_builder(distro, fn)` 注册;
  `build_base(distro="alpine", **kw)` 分发,缺省 alpine=旧行为)。
  - `build_alpine`(注册名 `alpine`,引用实现):live ISO -> `setup-disk -m sys` -> `apk add --root` 装包 ->
    烘焙 ttyS0 autologin + tun + 任意文件。raw 盘。
  - `build_fedora_cloud`(注册名 `fedora-cloud`):下载 Fedora cloud qcow2 + cloud-init seed + SSH provision 后关机。
- **QEMU VM**(`QemuVM`,已泛化,全部可选参数缺省=旧行为):
  - 盘:`[path,...]`(raw)或 `[{"path","format","if","index",...}]` 混合;**qcow2 覆盖盘**由 QEMU 自动读 backing。
  - 机器:`machine="q35"` 等;`-enable-kvm/-cpu/-display/-no-reboot/-boot` 可配。
  - 串口:`serial="file:/x.log"`/`"none"` 或缺省 unix socket(`serial_sock`)。
  - 网络:`nics[].hostfwd` 追加进 user netdev(**hostfwd 进 SSH**);仍支持 `socket,mcast` 虚拟总线。
  - 生命周期:`daemonize=True, pidfile=...`(经 `-daemonize -pidfile`,kill/status 读 pidfile)。
  - `QemuVM.from_spec(name, dict)` 从 dict 构造。
- **通用辅助**:`create_overlay()`(qcow2 写时复制覆盖盘)、`port_free()`(hostfwd 端口预检)、
  `QemuVM` 上下文管理器与 `wait_exit()`、`SshConsole.reboot_and_wait()`(重启→等掉线→回连→校验)、
  `provision_cloud_vm()`(起 cloud 镜像 VM + SSH provision + 关机清理)。
- **日志过滤**:内置 `logfilter()` 函数与 `vmkit.py logfilter` 子命令,合并 `\r` 进度条重绘、按等级过滤、给结果标记着色。
- **起 N 个 VM**(`VMGroup`):从 base 稀疏克隆,挂虚拟 L2 总线/显式 MAC。仍可用。
- **编排**(两种并存):
  - 串口 `Console`/`login_alpine`/`run_cmd`:unix-socket,expect/send,marker 取 rc,去回显。OS 无关。
  - **SSH `SshConsole`**:`connect/run/scp_send/scp_recv`(hostfwd 进 guest sshd);`strict_host_checking`
    默认 True,仅对一次性测试 VM 显式关。
- **生命周期**:`VMGroup.launch/stop`;`QemuVM` 单 VM 亦可。

## 核心 API(`vmkit.py`)
```python
import vmkit
# 构建 base(Alpine;iso 取自 $VMKIT_ISO/.env 或传 iso=)
vmkit.build_base(out_raw="disks/base.raw", packages=["frr","dnsmasq"],
                 bake_files=[("/path/to/bin","/usr/local/bin/bin")])
# 或 Fedora cloud(演示可插拔构建器)
vmkit.build_base(distro="fedora-cloud", out_qcow2="disks/base.qcow2", release=44)
# 注册自定义发行版构建器
def my_builder(*, out_img, ...): ...
vmkit.register_builder("myos", my_builder)
# 起一组 VM
g = vmkit.VMGroup("disks/base.raw", "disks", "logs")
g.launch({"vm1": {"mem":256,"smp":1,"nics":[("socket,id=lan,mcast=230.0.0.1:1234","52:54:00:00:00:01")]}})
# 串口跑命令
con = vmkit.Console("logs/vm1.sock"); con.connect(); vmkit.login_alpine(con)
out, rc = vmkit.run_cmd(con, "uname -a")   # out 已去命令回显
g.stop()
# 泛化 QemuVM:qcow2 覆盖盘 + hostfwd + q35 + daemonize/pidfile
vm = vmkit.QemuVM("v", disks=[{"path":"/tmp/ovl.qcow2","format":"qcow2"}],
                  nics=[{"netdev":"user,id=n0","hostfwd":"hostfwd=tcp:127.0.0.1:2222-:22"}],
                  machine="q35", daemonize=True, pidfile="/tmp/v.pid",
                  serial="file:/tmp/v.serial")
vm.start(); vm.is_alive(); vm.kill()
# 上下文管理器:退出自动 kill
with vmkit.QemuVM("v2", disks=[...]) as vm2:
    vm2.start(); ...
# 通用辅助
vmkit.create_overlay("disks/base.qcow2", "disks/ovl.qcow2")
vmkit.port_free("127.0.0.1", 2222)          # True=可绑定
out, rc = vmkit.provision_cloud_vm(out_qcow2="disks/base.qcow2", host_port=2222,
                                   ssh_user="zfsbuild", seed_iso="seed.iso",
                                   provision_script="provision.sh")
# SSH 编排(hostfwd 进 guest sshd;strict_host_checking 默认 True)
s = vmkit.SshConsole("127.0.0.1", port=2222, user="zfsbuild",
                     keyfile="/k", strict_host_checking=False)
s.connect(); out, rc = s.run("uname -r"); s.scp_send("/a", "/tmp/a")
s.reboot_and_wait(verify="uname -r")
```

## CLI
```bash
python3 vmkit.py exec <sock> '<cmd>'     # 串口(旧接口,向后兼容)
python3 vmkit.py exec --ssh [--port] [--user] [--key] [--insecure] <host> '<cmd>'
python3 vmkit.py qemu start <name> <spec.json>   # 起 VM(spec 字段同 QemuVM)
python3 vmkit.py qemu stop <name> [--pidfile]
python3 vmkit.py qemu status <name> [--pidfile]  # exit 0=running 1=stopped
python3 vmkit.py launch <group.json>     # 起一组 VM
python3 vmkit.py stop <group.json>
python3 vmkit.py build <distro> <spec.json>      # 分发 build_base
python3 vmkit.py provision-cloud <spec.json>     # 起 cloud 镜像 VM 跑 provisioning
python3 vmkit.py logfilter <brief|normal|verbose> < log   # 终端日志分级过滤
python3 vmkit.py status [--dir DIR]      # 扫 *.pid 列运行中 VM
```

## 默认值(可覆盖)
- **ISO**:`$VMKIT_ISO`(.env 或 export)或 `build_base(iso=...)`,**必填**(无默认机器路径)。
- apk 源:`$VMKIT_APK_REPOS` 覆盖;缺省 USTC v3.24 main+community。换版本改 URL 版本号,常用镜像见 `.example.env`。
- 网络:`socket,mcast` 虚拟总线(L2 hub,转发所有帧含多播);构建期用 slirp(静态 `10.0.2.15`,host=`10.0.2.2`)。

## 通用踩坑(已内置处理,供他场景参考)
- **`setup-disk -m sys` 的 lbu overlay 不带 `/etc/apk/world` 额外包** -> 包改 `apk add --root /mnt` 后装;
  非 /etc 文件(如二进制)同理由 `bake_files` post-copy(lbu 只备份 /etc)。
- busybox `pkill -x <name>` 常失效(用 `kill -9 $(pidof <name>)`);busybox getty 无 `-a` 自动登录
  (用 `getty -n -l /etc/autologin.sh` + `exec /bin/login -f root`);`udhcpc` 前台挂起(构建期静态配 slirp)。
- **串口驱动**:`re.search(bytearray)` 的 group 别名到原 buffer,`del` 后失效变空 -> 须在 `bytes(buf)` 副本上 search。
- **OSPFv3 等需唯一链路本地**:多 VM 默认 MAC 碰撞 -> fe80:: DAD 失败 -> 给每 NIC 显式 MAC。
  且 QEMU 拒绝单字符十六进制段(`52:54:00:00:00:a` 非法),必须两位(`52:54:00:00:00:0a`)。
- **非 Alpine 发行版**:`login_alpine` 是 Alpine 登录流程;其他发行版需自行实现等价的 `login_*`。
  串口驱动(`Console`/`run_cmd`)本身 OS 无关。

## 环境要求
host:Linux + `/dev/kvm`(用户可写)+ `qemu-system-x86_64` + `python3`。Alpine virt ISO(经 `.env` 的 `VMKIT_ISO` 指向)。
