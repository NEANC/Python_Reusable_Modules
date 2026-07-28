> [!WARNING]
> 本项目使用 TRAE IDE 生成与迭代

> [!CAUTION]
> 请注意：由 AI 生成的代码可能有：不可预知的风险和错误！  
> 如您需要直接使用本项目，请**审查并测试后再使用**；  
> 如您要将本项目引用到其他项目，请**重构后再使用**。

---

# Python Reusable Modules

基于 M9A_Update_Assistant 与 ALAS_Logs_Archive 项目实践的 Python 可复用模块。

---

### download

根目录 `download/` 是独立下载模块，可被外部直接使用：

```python
from download import DownloadManager

manager = DownloadManager(proxy="", temp_folder="./tmp", logger=logger)
manager.download_file_with_progress(url, save_path)
```

`self_updater` 的默认 exe 下载依赖该模块。复制或引入 `self_updater` 时，如果使用默认下载能力，需要同时包含 `download/`；如果传入自定义 `download_func`，则可完全覆盖默认下载行为。

---

## License

[WTFPL](./LICENSE)