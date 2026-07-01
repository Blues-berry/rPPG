# 直播心率补光插件

这个目录是第一阶段 OBS + B站直播原型。它不再让网页负责摄像头采集，而是由 `model_server.py` 常驻打开摄像头、持续送入当前仓库的 `rppg.Model('FacePhys.rlap')`，控制台和 OBS Browser Source 只订阅服务端状态。

## 页面

* `index.html`：主播/调试控制台，负责启动/停止后端采集、查看 HR/SQI、补光建议和链路性能。
* `camera.html`：OBS 摄像头画面源，读取服务端 MJPEG 预览，避免 OBS 与模型服务抢占摄像头。
* `overlay.html`：透明心率 Overlay，可作为 OBS Browser Source 添加到直播画面。
* `model_server.py`：本地 Open-rppg 常驻采集与模型服务，使用 `rppg.Model('FacePhys.rlap')` 处理摄像头帧。

## 运行

在仓库根目录启动本地模型服务：

```bash
.\.venv\Scripts\python.exe demo\live-heart-light-plugin\model_server.py
```

打开控制台：

```text
http://127.0.0.1:8020/
```

OBS Browser Source 添加两个源：

```text
摄像头画面：http://127.0.0.1:8020/camera.html
心率 Overlay：http://127.0.0.1:8020/overlay.html
```

## 演示路径

1. 启动 `model_server.py`，打开控制台页面。
2. 点击“启动后端采集”，Python 服务独立打开摄像头。
3. 服务端持续调用 `rppg.Model.update_frame()`，复用仓库自带 BlazeFace/ONNX 检脸与追踪。
4. `rppg.Model.hr(start=-10)` 返回 HR/SQI 后，服务端生成统一 Overlay 状态。
5. OBS 使用 `camera.html` 显示主播画面，使用 `overlay.html` 显示心率。

## 当前模型链路

* 主路径：Python 摄像头采集 -> Open-rppg BlazeFace/追踪 -> `rppg.Model('FacePhys.rlap')` -> HR/SQI -> Overlay。
* BPM 范围：使用 Open-rppg `get_hr` 默认配置 `30-180 BPM`。
* 输出门控：摄像头停止、无人脸、SQI 低于阈值时，Overlay 输出空值，不沿用旧 BPM。
* 低 SQI 策略：控制台可以显示低置信 BPM 预览，但 Overlay 只有 `SQI >= 0.38` 后才输出。
* 全时段采集：摄像头采集线程在 Python 服务内运行，控制台页面关闭、切屏或最小化不会中断模型输入。
* 性能面板：显示服务端采集 FPS、采集耗时、模型输入 FPS、模型 update 耗时、HR 计算耗时、无人脸和丢帧次数。

## OBS + B站接入

第一阶段建议主播使用 OBS 推流到 B站。OBS 场景中不要再直接添加同一个物理摄像头，而是添加 `camera.html` 作为摄像头画面源，再叠加 `overlay.html`。这样摄像头由 Python 服务独占，rPPG 监测不会因为浏览器切后台而断链。

## 下一步

1. 支持选择摄像头设备、分辨率和帧率。
2. 对接补光灯硬件，测量时锁定补光，非测量时允许氛围灯效。
3. 加入服务启动器，减少主播手动运行命令的成本。
4. 第二阶段再考虑 OBS 原生插件或虚拟摄像头版本，用于接入抖音、快手、小红书等闭源直播工具。
5. 如果未来需要无后端网页版本，需要先把 rPPG 模型导出为 ONNX/TF.js/WebGPU/LiteRT 可执行格式。
