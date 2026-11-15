# 集成边界

## 接收接口

核心服务具有 OneBot v11 HTTP 事件接收能力。生产接入必须由使用者另行配置合法可用的采集端、群白名单与认证；此公开演示入口不启动采集器或监听 QQ 事件。

当前私人环境的共存方案使用 SnowLuma 2.0.3。核对的本地许可证为 SnowLuma Source-Available Non-Commercial License，明确区分其原生组件，限制修改版公开发布。仓库因此不包含该采集器、修改版 bundle、原生库、其配置或补丁分发包。只读、原账号共存是集成目标，不代表本仓库提供了可直接安装的采集产品。

早期调研包含 NapCatQQ / NapCat Desktop 与 Renecon；最终工作台引擎来自 qq-live-digest。不得把其他项目的实现归为本项目原创。

## 私人连接

电脑与手机通过私人连接访问工作台。Tailscale 的安装包、登录身份与设备密钥不纳入仓库。允许联网访问，不等于电脑关机时仍有服务运行。

## Android 容器

`android/WorkbenchActivity.java` 为工作台容器源码，涉及可信 HTTPS 来源、VPN 连接请求、恢复前台、文件选择与下载。它依赖原 Android 应用环境，不是完整 Gradle 项目，不能单独构建 APK。未附带第三方 APK、反编译资源、原生库或本人的签名证书。

后续若计划重新分发完整 APK，需要另行整理可复现源码、构建环境和相关许可证声明。
