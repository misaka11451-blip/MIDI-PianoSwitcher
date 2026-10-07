# pianoize Web 静态前端

`index.html`、`app.css`、`app.js` 这三个文件由 Python 后端（`pianoize.web`）作为静态资源**原样发出**，没有构建步骤、没有模板渲染、没有任何外部依赖 —— 全部使用相对路径互相引用，因此整个界面可以完全离线运行。

页面通过同源相对路径调用后端接口：`POST /api/project`（上传音轨）、`POST /api/preview`（试听）、`POST /api/render`（完整渲染）、`GET /api/files/{project_id}/{filename}`（下载/播放产物）。只有当直接用 `file://` 打开 `index.html` 调试时，才会回退到 `index.html` 里 `<meta name="pianoize-backend">` 配置的地址（默认 `127.0.0.1:8765`，该模式下需要后端返回 CORS 头）。

改主题配色只需要改 `app.css` 里 `:root` 的 CSS 变量：波形画布的配色也是运行时从这些变量里读的。
