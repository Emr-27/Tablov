"""双击入口：隐藏终端，将错误写入本机日志。"""
from pathlib import Path
import sys
import traceback

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
log_dir = root / "runtime" / "ui"
log_dir.mkdir(parents=True, exist_ok=True)
with (log_dir / "server.log").open("a", encoding="utf-8", buffering=1) as log:
    sys.stdout = log
    sys.stderr = log
    try:
        from harmonica_transcriber.webui import main
        main()
    except Exception:
        traceback.print_exc()
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, "启动失败，请查看：\n" + str(log_dir / "server.log"), "扒谱洛夫", 16)
