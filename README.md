# vmkit - 无头 Alpine KVM VM 工具包

单文件 Python 库(`vmkit.py`,零依赖、零打包)。在 Linux host 上用 **rootless KVM + QEMU** 构建 base 镜像、
起若干无头 Alpine VM,经**串口**无头编排(登录、跑命令、做断言),适合任何"需要真实内核/网络栈、又不想动 host"
的测试场景。零 host 网络影响(无 host bridge/tap、无 root)。

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
- **构建 base 镜像**(`build_base`):live ISO -> `setup-disk -m sys` -> `apk add --root` 装自定义包 ->
  烘焙 ttyS0 autologin + tun 模块 + 任意额外文件(二进制等)。raw 盘(非 qcow2)。
- **起 N 个 VM**(`VMGroup`):从 base 稀疏克隆,挂 `socket,mcast` 虚拟 L2 总线(或 slirp),
  显式 MAC。也可直接用 `QemuVM` 起单个 VM(可经 `fw_cfg` 注入任意文件)。
- **串口编排**(`Console`/`login_alpine`/`run_cmd`):unix-socket 串口,expect/send,marker 取 rc,
  自动去命令回显。无 mgmt NIC--纯拓扑,镜像真实硬件。
- **生命周期**:`VMGroup.launch/stop`,vms.pid 管理。`stop()` 仅按本组实际起过的 VM 名清理,
  不含任何用例专属名,可安全用于任意 VM 命名。

## 核心 API(`vmkit.py`)
```python
import vmkit
# 构建 base(自定义包 + 二进制;iso 取自 $VMKIT_ISO/.env 或传 iso=)
vmkit.build_base(out_raw="disks/base.raw",
                 packages=["frr","dnsmasq","python3","curl"],
                 bake_files=[("/path/to/binary","/usr/local/bin/binary")])
# 起一组 VM
g = vmkit.VMGroup("disks/base.raw", "disks", "logs")
g.launch({"vm1": {"mem":256,"smp":1,"nics":[("socket,id=lan,mcast=230.0.0.1:1234","52:54:00:00:00:01")]},
          "vm2": {"mem":256,"smp":1,"nics":[("socket,id=lan,mcast=230.0.0.1:1234","52:54:00:00:00:02")]}})
# 串口跑命令
con = vmkit.Console("logs/vm1.sock"); con.connect(); vmkit.login_alpine(con)
out, rc = vmkit.run_cmd(con, "uname -a")   # out 已去命令回显
g.stop()
```
CLI:`python3 vmkit.py exec logs/vm1.sock 'ip -br a'` -- 对运行中 VM 跑一条命令(调试)。

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
