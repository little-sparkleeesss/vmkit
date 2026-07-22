# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

vmkit 是**单文件、零依赖**的 Python 库(`vmkit.py`),在 Linux host 上用 **rootless KVM + QEMU** 构建 base
镜像、起若干无头 Alpine VM,经**串口(unix socket)**做编排(登录、跑命令、做断言)。定位是"需要真实内核/网络栈、
又不想动 host"的测试场景--零 host 网络影响(无 bridge/tap、无 root)。详细用法见 `README.md`。

## 常用命令

无构建系统/无 linter/无测试框架--纯 stdlib 单文件库。语法检查用 `python3 -m py_compile vmkit.py`。

- **构建 base 镜像**(无 CLI,写小脚本调用 `vmkit.build_base(...)`;ISO 路径取自 `$VMKIT_ISO`/`.env` 或 `iso=`
  参数,二者皆无则报错):
  ```python
  import vmkit
  vmkit.build_base(out_raw="disks/base.raw",
                   packages=["frr","dnsmasq","python3","curl"],
                   bake_files=[("/path/to/bin","/usr/local/bin/bin")])
  ```
- **起一组 VM + 串口操作**(写小脚本调用 `VMGroup`/`Console`;需先构建/备好 base 镜像):
  ```python
  import vmkit
  g = vmkit.VMGroup("disks/base.raw", "disks", "logs")
  g.launch({"vm1": {"mem":256,"smp":1,"nics":[("socket,id=lan,mcast=230.0.0.1:1234","52:54:00:00:00:01")]}})
  con = vmkit.Console("logs/vm1.sock"); con.connect(); vmkit.login_alpine(con)
  out, rc = vmkit.run_cmd(con, "uname -a")
  g.stop()
  ```
- **对运行中 VM 跑一条命令**(调试):`python3 vmkit.py exec logs/<name>.sock 'ip -br a'`
- **配置**:`cp .example.env .env`,填 `VMKIT_ISO`(Alpine virt ISO 路径);apk 源可选 `VMKIT_APK_REPOS`(完整 URL 列表,换镜像/版本改这里,常用镜像见 `.example.env` 注释),缺省 USTC v3.24。`.env` 由 `.gitignore` 排除。

## 架构(big picture)

`vmkit.py` 做三件事,串联成"构建 -> 起 -> 操作":

1. **构建 base 镜像**(`build_base`):live ISO 起来 -> 静态 slirp 联网 -> `setup-disk -m sys` 装到 `/dev/vda` ->
   **`apk add --root /mnt`** 装自定义包(关键:`setup-disk` 的 lbu overlay 不带 `/etc/apk/world` 额外包,所以包必须
   在 setup-disk 之后用 `--root` 装)-> 烘焙 ttyS0 autologin + tun 模块 + `bake_files`(经 host HTTP 投递再 post-copy)。
   产出 raw 盘(非 qcow2)。
2. **起 VM**:`QemuVM`(单个,通用--支持 raw 盘、virtio NIC、可选 `fw_cfg` 注入任意文件)+ `VMGroup`(一组:从 base
   盘稀疏克隆 `cp --sparse=always`,挂 `socket,mcast` 虚拟 L2 总线转发所有帧含多播,镜像真实硬件;给每 NIC 显式 MAC;
   记 `logs/vms.pid`)。无 mgmt NIC。
3. **串口操作无头 VM**:`Console`(unix-socket,expect/send)+ `login_alpine`(Alpine 登录流程,兼容 base autologin
   与 live ISO)+ `run_cmd`(发命令,等时间戳 RC marker,返回去回显输出 + exitcode)。

`VMGroup.stop` 仅按本组实际起过的 VM 名 + pidfile 兜底清理(不含任何用例专属名),可安全用于任意 VM 命名。

## 关键约束/踩坑(改代码前必读)

- **`setup-disk -m sys` 的 lbu overlay 不带额外包**:自定义包必须 setup-disk 之后 `apk add --root /mnt` 装;
  非 /etc 文件(二进制)经 `bake_files` post-copy(lbu 只备份 /etc)。
- **串口驱动 search 必须在 `bytes(con.buf)` 副本上做**:`re.search(bytearray)` 的 group 别名到原 bytearray,
  随后 `del` 会使 group 失效变空(`run_cmd` 里有注释,别"优化"掉这个 copy;已实测确认)。
- **每 NIC 必须显式 MAC 且为两位十六进制段**:多 VM 默认 MAC 碰撞 -> fe80:: DAD 失败 -> OSPFv3 等链路本地
  协议起不来;且 QEMU 拒绝单字符段(如 `52:54:00:00:00:a`,已实测),必须 `52:54:00:00:00:0a`。
- **busybox 限制**:无 `pkill -x`(用 `kill -9 $(pidof ...)`)、getty 无 `-a`(用 `-n -l /etc/autologin.sh` +
  `exec /bin/login -f root`)、`udhcpc` 前台挂起(构建期静态配 slirp)。
- **非 Alpine 发行版**:`login_alpine` 是 Alpine 登录流程;其他发行版需自行实现等价 `login_*`(`Console`/`run_cmd`
  本身 OS 无关)。
- **`.env` 语义**:`os.environ.setdefault`,不覆盖已 export 的变量;从 CWD 向上查找。

## 环境要求

host:Linux + `/dev/kvm`(用户可写)+ `qemu-system-x86_64` + `python3`。Alpine virt ISO(经 `.env` 的 `VMKIT_ISO` 指向)。

运行 VM / 构建镜像的命令(`build_base` / `VMGroup.launch` / 直接 `QemuVM`)需要 host 的 `/dev/kvm` 访问,
且 `build_base` 会在 host 起一个临时 HTTP server(127.0.0.1:8080)投递文件--这些是设计上直接跑在 host 上的;
若按全局偏好用 rootless podman 隔离,需 `--device /dev/kvm` 透传并挂载 `disks/`、`logs/`。纯改源码/`py_compile`
无副作用,任意环境均可。
