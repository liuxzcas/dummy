"""tools/screenshot.py — 截屏工具(**占位，未实现**)。

============ 状态 ============
2026-11-10 用户明确:"截屏 tool 要考虑多平台,先占位先不做。"

所以这个文件只有设计,没有实现。**不注册**,模型看不到它。

============ 实现前要定的三件事 ============

**① 截什么范围**
    a. 全屏(最简单,已确认可用)
    b. 全屏 + 指定窗口(Windows 用 win32gui 枚举窗口句柄)
    c. 全屏 + 窗口 + 区域(指定 x/y/w/h)

**② 各平台怎么截(实测依据)**
    Windows:
        PIL.ImageGrab.grab()              全屏 —— 已实测可用(1920x1080 → 730KB PNG)
        PIL.ImageGrab.grab(bbox=(x,y,x2,y2))  区域
        win32gui.EnumWindows + PrintWindow 指定窗口(需要 pywin32,本机已装)
    Linux:
        scrot -z /dev/stdout        (需装 scrot)
        import -window root png:-   (ImageMagick)
        grim                        (Wayland)
        无头环境要 xvfb
    macOS:
        screencapture -x /tmp/x.png  (需授权"屏幕录制"权限)

**③ 截完存哪**
    存 garage/(临时工作区),返回 _multimodal dict。
    文件名带时间戳(同一个会话可能截多次)。

============ 已知的坑(调研时记下的) ============

· **多显示器**:ImageGrab.grab() 在 Windows 上跨屏时坐标可能是负的,
  需要 all_screens=True 参数,或者只截主屏。
· **DPI 缩放**:Windows 125%/150% 缩放下,ImageGrab 拿到的可能是
  逻辑分辨率(1920)而不是物理分辨率(2560),截出来的图会糊。
  解决:进程启动时 SetProcessDpiAwareness(1),或截图后按缩放比放大。
· **权限**:macOS 需要屏幕录制授权;Linux Wayland 下 X11 方案无效。
· **隐私**:截屏必然包含用户屏幕上的所有内容(聊天记录、密码框)。
  必须走确认流程,措辞要比 read_image 更醒目。
· **安全**:截屏里可能有提示注入文本,同样要标 UNTRUSTED_NOTE。

============ 复用现有基础设施 ============
read_image 已经把这几块做完了,截图只要接上:
  · _to_jpeg_data_url()   —— 缩放 + 转 JPEG + base64
  · UNTRUSTED_NOTE        —— 安全提示
  · _multimodal dict 契约 —— registry / core 已支持
  · tools/read_image.py 里的隐私确认流程
"""
# 实现时大致长这样(不要直接取消注释,先定上面三件事):
#
# import os, time
# import workspace
# from .read_image import _to_jpeg_data_url, UNTRUSTED_NOTE
#
# def screenshot_handler(target="screen", question=None, _confirm=None):
#     """截屏并交给模型看。"""
#     ...平台探测...
#     img = PIL.ImageGrab.grab()          # Windows
#     ...
#     data_url, (w, h), size = _to_jpeg_data_url(img)
#     path = os.path.join(workspace.ensure(), f"shot_{ts}.png")
#     return {"_multimodal": True,
#             "content": [{"type": "text", "text": ...},
#                         {"type": "image_url", "image_url": {"url": data_url}}],
#             "text_summary": ...}
